# hardrail reference

Detail that the [README](../README.md) only summarises. Every statement here is backed by a
file in this repository; the file is named in each section.

## Contents

- [One Bash call, step by step](#one-bash-call-step-by-step)
- [Fail-open and fail-closed](#fail-open-and-fail-closed)
- [Rules](#rules)
- [Runner detection](#runner-detection)
- [Verdict table](#verdict-table)
- [Configuration keys](#configuration-keys)
- [Monorepo resolution](#monorepo-resolution)
- [Latency](#latency)
- [Design notes](#design-notes)
- [Repository layout](#repository-layout)

## One Bash call, step by step

1. **bash pre-filter** (`adapters/claude/pre-bash.sh`). Reads the hook's JSON from stdin,
   takes everything after the first `"command":` key and runs a keyword `case` on it. No
   gated keyword, and no quote, backslash, `$`, backtick, `<(`, shell name, `xargs` or `parallel` (any of them can
   hide a keyword: `pu''sh`, `$cmd`) → `exit 0` without starting python. The keyword list must cover the first
   word of every gated and heavy entry; a test checks it against the defaults.
2. **python shim** (`adapters/claude/pre_bash.py`, 30 lines or fewer). Parses the JSON, finds
   the nearest `hardrail.config.json` above `cwd`, calls `decide`, maps the decision to an
   exit code and a stderr line.
3. **tokenizer** (`core/shell.py`). Splits the command on `;`, `&&`, `||`, `|`, `&` and
   newlines, quote- and comment-aware. Text that another shell would execute is parsed as
   its own chain ("payload"): `sh -c "…"`, `eval …`, a heredoc or here-string fed to
   `bash`/`sh`/`zsh`/`dash` (directly or through a pipe), `source` / `.` of stdin (`-`,
   `/dev/stdin`, a here-string), `builtin eval` / `command eval`, `$(…)`, backticks,
   process substitution `<(…)` / `>(…)`, and the text a command runner hands to a shell:
   `watch …`, `ssh <host> …`, `parallel … ::: …`, `flock -c …`, `script -c …`.
4. **rules** (`core/guard.py`), in a fixed order; the first hit wins.
5. **package resolution** (`core/packages.py`) for the test-scoping rule, see
   [Monorepo resolution](#monorepo-resolution).

Claude Code shows the agent only stderr on exit 2, so the message is the whole feedback:
every block line ends with the command to run instead.

## Fail-open and fail-closed

Exactly as the code does it:

| Situation | Result |
|---|---|
| shim raises (bad JSON, missing key, unexpected error) | stderr `hardrail: guard error: …`, exit 0 — the agent is never blocked by a bug in the guard |
| `python3` not on `PATH` | stderr `hardrail: python3 not found, guard disabled`, exit 0 |
| shim missing (`pre_bash.py` not next to `pre-bash.sh`) | stderr `hardrail: pre_bash.py not found, guard disabled`, exit 0 |
| event without a command (no `tool_input.command`, or an empty one) | exit 0, no output |
| `hardrail.config.json` is invalid JSON, not an object, or a section has the wrong type | stderr `hardrail: config ignored (…), using defaults`, rules still enforced |
| `hardrail.config.json` has an unknown top-level key | stderr `hardrail: unknown config key X ignored`, the rest of the file applies; `$schema` is accepted silently |
| command cannot be tokenized (unbalanced quote, unbalanced `$(`) | block, rule `unparsable` |
| heredoc body fed to a shell is over 200 lines or 8192 bytes | block, rule `unparsable`, message says to run it as a script file |
| substitutions nested deeper than 20 levels or over 8192 bytes in total | block, rule `unparsable` |
| test command targets a package that cannot be resolved | treated as "every package" → block, rule `unscoped_test` |

## Rules

Order, as `decide` runs it; the first that fires decides:

1. the tokenizer cannot split the command → `unparsable`;
2. `force_push`;
3. `xargs` fed by a pipe runs a command that is not read-only → `unparsable`;
4. `chained_gated`;
5. `watch_mode`;
6. `unscoped_test`;
7. `heavy`.

A rule that cannot read a command (an unmodelled wrapper option, an expanded `git push`
argument) stops there with `unparsable`. So `ls | xargs git push -f` reports `force_push`.

| Rule | What it catches | Blocks, for example | The message tells the agent to |
|---|---|---|---|
| `unparsable` | a command the tokenizer cannot split safely; an oversized shell payload; `xargs` fed by a pipe that runs a command which is not read-only | `pnpm test "oops`, `ls \| xargs rm` | split it or quote it plainly / write the command literally / run it as a script file / list the files explicitly; the message names the construct |
| `force_push` | `git push` with `--force`, `-f` or `-d` in a short-flag cluster, `--mirror`, `--delete`, a `+refspec` or a `:branch` refspec; `git` found by basename, git globals (`-C`, `-c`, `--git-dir`, …) skipped; also the command `npx`, `bunx`, `<manager> exec\|dlx\|x` or `find -exec\|-execdir\|-ok\|-okdir` runs. A `git push` argument that starts with `$(…)` or a `$` expansion → `unparsable`, since it could expand to `-f` or `+ref` (write the refspec literally); `HEAD:$BR` is allowed | `/usr/bin/git push --force origin main` | use `git push --force-with-lease` / ask before deleting remote refs |
| `chained_gated` | two or more commands in one call where any of them starts with a `guard.gatedActions` prefix; a gated action inside a payload counts when the whole command has more than one command | `git commit -m x && git push` | run `` `git commit -m x` `` as its own call |
| `watch_mode` | bare `vitest` without `--run`; `jest --watch`/`--watchAll`; an explicit `--watch` on a test script; `test:watch`; `docker compose logs -f`; `docker logs -f`; `tail -f`; `playwright`/`test:e2e` with `--ui`, `--watch` or `--debug` | `docker compose logs -f api` | `use docker compose logs --tail=200 api` (each case gets its own one-shot form) |
| `unscoped_test` | a test runner invocation whose own arguments carry no path, or one that spans every workspace package | `pnpm test` | `pnpm exec vitest --run <path/to/file.test.ts>` |
| `heavy` (warn only) | commands matching a `heavy` glob after the package-manager prefix is stripped | `pnpm build` | run it outside the agent loop or in a background task |

`guard.mode: "warn"` downgrades every block to a `WARN` line with exit 0, for a trial
period on an existing repo. `heavy` is always a warning.

Fixed edge cases (each blocks unless noted):

- `$'…'` and `$"…"` quoting → `unparsable`.
- A command word that is only a variable (`$cmd`, `${x}`, `"$@"`) → `unparsable`.
  `$HOME/bin/tool` (variable plus a literal path) is allowed.
- `<command> | bash`: literal `echo`/`printf` arguments are parsed as the payload, and a
  `cat` heredoc body is parsed as the payload; anything else piped into a shell, including
  `curl … | sh` and `echo $(…) | bash`, → `unparsable`.
- A shell reading a process substitution (`bash <(…)`, `bash < <(…)`, `source <(…)`,
  `. <(…)`) → `unparsable`.
- `watch`, `ssh`, `parallel`, `flock`, `script`, `caffeinate` and `unbuffer` with an
  option the guard does not model → `unparsable`; `parallel` with no command before `:::`
  (commands read from its input) → `unparsable`.
- A gated action inside a payload counts as chained whenever the whole command has more
  than one command; `bash -c "git commit -m x"` alone blocks, and the message says to run
  the inner command directly.
- `git push --delete`, `git push -d` and `git push origin :branch` → `force_push`, message
  "deleting a remote branch is blocked; ask before deleting remote refs".
- A path argument is a word with `/`, a `.test.`/`.spec.` name, or an existing file or
  directory relative to `cwd` (`vitest --run src` is allowed when `src/` exists). An empty
  word, `.`, `..`, or a `--testPathPattern` of `.`/`.*` is not a scope.

## Runner detection

Runner detection covers `pnpm`/`npm`/`yarn`/`bun` scripts named `test` or `test:*`,
`run test` (manager options after `run` skipped: `pnpm run -r test`), `exec`/`dlx`/`npx`/`bunx`
followed by `vitest` or `jest`, direct `vitest`/`jest` binaries, `turbo run test`, `turbo test`,
`nx test`, `nx run-many -t|--target[=]` with `test` among the targets, `yarn workspace <name> test`,
`yarn workspaces foreach`, the configured `defaults.test` command, and all of these behind
env-var prefixes (`CI=1 …`) or wrappers (`sudo`, `time`, `env`, `nohup`, `command`,
`exec`, `corepack`, `flock`, `unbuffer`, `caffeinate`, `script`).

## Verdict table

Every row below is a line of `test/core/guard_table.py` and is asserted in CI.

| Command | Verdict |
|---|---|
| `pnpm test` | block `unscoped_test` |
| `p"n"pm test` | block `unscoped_test` |
| `pnpm test # src/` | block `unscoped_test` |
| `bash -c "pnpm test"` | block `unscoped_test` |
| `x=$(pnpm test)` | block `unscoped_test` |
| `pnpm exec vitest --run src/a.test.ts` | allow |
| `CI=1 pnpm test src/a.test.ts` | allow |
| `npx jest src/a.test.ts` | allow |
| `vitest src/a.test.ts` | block `watch_mode` |
| `pnpm test -- --watch src/a.test.ts` | block `watch_mode` |
| `git commit -m x & git push` | block `chained_gated` |
| `git commit -m "fix; and more"` | allow |
| `git push -fu origin main` | block `force_push` |
| `git --git-dir=.git push --force` | block `force_push` |
| `git push --force-with-lease` | allow |
| `echo "git push --force"` | allow |
| `cat <<EOF \| bash` with body `git push --force` | block `force_push` |
| `cat <<EOF \| grep x` with the same body | allow |
| `find . -name "*.orig" -exec rm {} \;` | allow |
| `pnpm build` | warn `heavy` |

## Configuration keys

`hardrail.config.json` at the repository root; the nearest one above the command's `cwd`
is used. JSON Schema: `schema/hardrail.config.schema.json`. The file deep-merges over the
defaults: objects merge key by key, lists and scalars from the file replace the default.
An invalid file is ignored with a stderr warning and the defaults are enforced; a section
with the wrong type (for example `"guard": []`) reverts to its default the same way.

Keys read by the guard today (`core/config.py` `DEFAULTS`):

| Key | Type | Default | Meaning |
|---|---|---|---|
| `defaults.test` | string | `"pnpm exec vitest --run"` | the scoped test command; used in fix messages and recognised as a runner |
| `packages` | object | `{}` | `name → { test }`; a package `test` command is also recognised as a runner |
| `guard.mode` | `"block"` \| `"warn"` | `"block"` | `warn` downgrades every block to a warning |
| `guard.gatedActions` | string[] | `git commit`, `git push`, `git reset`, `git rebase`, `git merge`, `git stash`, `git clean`, `rm`, `pnpm install`, `pnpm add`, `pnpm remove`, `npm install`, `yarn add`, `docker`, `gh pr`, `prisma migrate`, `pnpm i`, `npm i`, `npm ci`, `yarn install`, `bun install`, `bun add` | command prefixes that must run as their own call |
| `heavy` | string[] | `build`, `build:*`, `turbo build`, `vite build`, `generate:*`, `prisma`, `prisma *`, `playwright`, `playwright *` | globs (`*` and `?` only) matched against the command after `pnpm`/`npm run`/`yarn`/`npx` is stripped (`turbo run <script>` the same way); an entry matches the command exactly or as a word prefix; a match warns |
| `worktree` | object | `{ "required": false, "dir": ".worktrees", "include": [] }` | reserved for the worktree guard (not read yet) |

The schema also accepts `ci`, `codegen`, `conventionChecks`, `modules`, `language`,
`$schema`, `defaults.lint` and `defaults.typecheck`. They belong to the
planned CI and `init` layers and are not read by any code in this release.

Lists in the file replace the default list; to add one entry, repeat the defaults and
append it. When `hardrail.config.json` exists at `$CLAUDE_PROJECT_DIR`, `pre-bash.sh`
skips its keyword fast path, so added gated or heavy entries reach python even when their
first word is not in the keyword list. Rendering that list from the config is on the
roadmap.

## Monorepo resolution

`unscoped_test` needs to know which package a test command targets, because a path
argument only scopes the run inside that package. Resolution order for a
`pnpm`/`npm`/`yarn`/`bun` command:

1. `-C <dir>` / `--dir <dir>` (also the `=` forms) → that directory.
2. `--filter <name>` / `-F <name>` → the workspace package with that `name` in its
   `package.json`; `--filter ./path` → that directory; `yarn workspace <name>` the same.
3. Every `cd` earlier in the same chain is applied in order (`cd packages/web; ls; pnpm
   test src/a.test.ts` targets `packages/web`). A subshell `(cd dir && pnpm test …)` is
   parsed as the same chain.
4. Otherwise the nearest ancestor of `cwd` with a `package.json`, never above the
   repository root.

Workspaces are read from `pnpm-workspace.yaml` (`packages:` list) or the `workspaces`
field of the root `package.json`.

These forms mean "every package" and block even when a path is given: `-r`,
`--recursive`, `--parallel`, `-w`/`--workspace-root` when the root package has no
scoped script, a filter containing `*`, `...`, `[`, `{` or a leading `!`, more than
one `--filter`, `yarn workspaces foreach`, an unknown package name, a `-C` directory
without `package.json`, or a directory outside the repository. The fix shown is
`pnpm --filter <name> exec vitest --run <path>`.

When the target package's own `test` script already carries a path (for example
`"test": "vitest src/"`), `pnpm test src/a.test.ts` does not narrow the run — both become
file filters and the whole `src/` directory still runs. The guard blocks and shows the
direct form instead: `pnpm -C <dir relative to cwd> exec vitest --run <path>` (just
`pnpm exec vitest --run <path>` when `cwd` is already the package).

## Latency

The hook runs on every Bash call, so it must be cheap:

- **bash fast path** — a command with no gated keyword never starts python.
- **python path** — `python3 -I -S` with a core that imports only `os`, `re`, `shlex`,
  `json`, `glob`, `copy`; no `dataclasses`, `typing` or `fnmatch`, whose import cost is a large
  share of a cold start (check with `python3 -X importtime`).
- **payload caps** — a heredoc body handed to a shell is parsed only up to 200 lines /
  8192 bytes; substitutions are parsed up to 20 levels deep / 8192 bytes in total. Larger
  payloads are blocked as `unparsable` instead of parsed.
- **CI gate** — `test/bench_guard.py` runs four cases (fast path, python path, a 20 KB
  `cat` heredoc, the cap path) through the real hook in three batches of 50 runs, asserts
  each case's exit code, and fails when the best batch p95 of any case is 100 ms or more.
  It runs on `ubuntu-latest` and `macos-latest` on every push.

Local numbers depend on the machine's load and python build; a busy laptop exceeds the
gate. CI is the reference.

## Design notes

- **Standard library only.** The hook runs on every tool call in every clone; it cannot
  depend on a virtualenv, a package manager or a Node install.
- **Repo first, plugin second.** Everything the guard needs is committed to the adopting
  repository. A plugin would only automate installation; if it is absent, the guard still
  runs.
- **Core / adapter split.** `decide(command, cwd, config)` is the whole policy. An adapter
  only knows how its agent delivers the command and consumes the verdict.
- **The tokenizer owns quoting and comments.** Rules see argv lists, never raw text, so a
  quoted `"#1"` is an argument and an unquoted `#` ends the command. Everything another
  shell would execute (`sh -c`, `eval`, heredocs, substitutions) is parsed as its own chain.
- **Fail open for bugs, fail closed for doubt.** A crash in the guard must not block the
  agent; a command the guard cannot read must not run.
- **The pre-filter must cover every gated and heavy first word.** A keyword missing from
  the bash `case` is a silent bypass, so a test enumerates the defaults against it.
- **Every block message carries the corrected command.** The agent sees only stderr; a
  bare "blocked" costs a round-trip and teaches nothing.
- **No numbers without evidence.** The design is extracted from a private workspace; only
  mechanisms travel. The docs state only what this repository's tests and commands
  reproduce.

## Repository layout

```
core/            shell.py (tokenizer) · guard.py (rules, decide) · packages.py (workspace resolution)
                 config.py (defaults, loader) · argv.py (env/wrapper/option helpers)
adapters/claude/ pre-bash.sh · pre_bash.py · hooks.snippet.json
schema/          hardrail.config.schema.json
test/            core/ (table + unit tests) · adapters/ · fixtures/ · bench_guard.py · test_no_leak.py
docs/            reference.md · overview.md · superpowers/specs/ · superpowers/plans/
.github/         workflows/ci.yml (unit tests + bench on ubuntu and macos)
```
