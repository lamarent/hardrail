# hardrail

Hook-enforced guard rails for AI coding agents. A repository adopts hardrail by committing
a few files; every teammate who clones the repo gets the same guard, and the agent cannot
talk its way around it.

The problem it solves: rules written as prose (`CLAUDE.md`, `AGENTS.md`, review
checklists) are still violated, most often on exactly the commands the prose spells out —
an unscoped test run that takes minutes, a watch-mode runner that never exits, a
`git commit && git push` chain that cannot be reviewed in between. A pre-tool hook that
blocks the command and prints the corrected form removes the option instead of lowering
the odds.

The core is agent-agnostic (python3 ≥ 3.9, standard library only, no Node, no `jq`).
Claude Code is the first adapter, not the design. It is written for TypeScript
repositories that use `pnpm` / `npm` / `yarn` / `bun` with `vitest` or `jest`, including
pnpm workspaces.

## How it works

Every rule is enforced at the lowest tier that actually holds:

| Tier | Mechanism | Holds because |
|---|---|---|
| prose | rule files the agent reads | nothing; it is advice |
| hook | a `PreToolUse` command that exits 2 | the tool call never runs |
| CI | a workflow on the pull request | merges are blocked |

hardrail ships the hook tier today (see [Status](#status--roadmap) for the rest). The
target shape of the whole system, from `docs/overview.md`:

```
 ┌─ 1. ADOPT ───────────────────────────────────────────────────────────────────┐
 │  hardrail init                                                               │
 │    detect lockfile · package.json · workspaces · biome/eslint · lefthook     │
 │    → hardrail.config.json  (the one project-specific file)                   │
 │    → .claude/hardrail/{core,pre-bash.sh}  rules/hardrail/  skills/hardrail-* │
 │    → .github/workflows/hardrail.yml       .hardrail-manifest.json            │
 │    existing file? → <name>.hardrail-new, never overwritten                   │
 └──────────────────────────────────────────────────────────────────────────────┘
                                   │ committed → every teammate gets it by clone
                                   ▼
 ┌─ 2. RUN (per session) ───────────────────────────────────────────────────────┐
 │  prompt ──► agent ──► Bash tool call                                         │
 │                           │ PreToolUse                                       │
 │                      pre-bash.sh  fast path: no gated keyword → exit 0       │
 │                           │ else python3 -I -S pre_bash.py                   │
 │                           ▼                                                  │
 │                  core.decide(cmd, cwd, config)                               │
 │                  unparsable → force_push → chained_gated → watch_mode →      │
 │                  unscoped_test → heavy                                       │
 │                           │                                                  │
 │        allow: exit 0      warn: stderr WARN, exit 0                          │
 │        block: stderr "BLOCKED [rule]: <what to run instead>", exit 2         │
 │                           │ agent retries with the corrected command         │
 └──────────────────────────────────────────────────────────────────────────────┘
                                   │ git push → PR
                                   ▼
 ┌─ 3. CI ──────────────────────────────────────────────────────────────────────┐
 │  scoped tests from the diff · typecheck · codegen in sync · self-check       │
 └──────────────────────────────────────────────────────────────────────────────┘
                                   ▼
 ┌─ 4. IMPROVE ─────────────────────────────────────────────────────────────────┐
 │  count blocks per rule from transcripts → promote or drop a rule → commit    │
 └──────────────────────────────────────────────────────────────────────────────┘
```

Only box 2 is implemented so far. The sections below describe that box.

### One Bash call, step by step

1. **bash pre-filter** (`adapters/claude/pre-bash.sh`). Reads the hook's JSON from stdin,
   takes everything after the first `"command":` key and runs a keyword `case` on it. No
   gated keyword → `exit 0` without starting python. The keyword list must cover the first
   word of every gated and heavy entry; a test checks it against the defaults.
2. **python shim** (`adapters/claude/pre_bash.py`, under 30 lines). Parses the JSON, finds
   the nearest `hardrail.config.json` above `cwd`, calls `decide`, maps the decision to an
   exit code and a stderr line.
3. **tokenizer** (`core/shell.py`). Splits the command on `;`, `&&`, `||`, `|`, `&` and
   newlines, quote- and comment-aware. Text that another shell would execute is parsed as
   its own chain ("payload"): `sh -c "…"`, `eval …`, a heredoc or here-string fed to
   `bash`/`sh`/`zsh`/`dash` (directly or through a pipe), `$(…)`, backticks, and process
   substitution `<(…)` / `>(…)`.
4. **rules** (`core/guard.py`), in a fixed order; the first hit wins.
5. **package resolution** (`core/packages.py`) for the test-scoping rule, see
   [Monorepo support](#monorepo-support).

Fail-open and fail-closed, exactly as the code does it:

| Situation | Result |
|---|---|
| shim raises (bad JSON, missing key, unexpected error) | stderr `hardrail: guard error: …`, exit 0 — the agent is never blocked by a bug in the guard |
| `python3` not on `PATH` | stderr `hardrail: python3 not found, guard disabled`, exit 0 |
| `hardrail.config.json` is invalid JSON or not an object | stderr `hardrail: config ignored (…), using defaults`, rules still enforced |
| command cannot be tokenized (unbalanced quote, unbalanced `$(`) | block, rule `unparsable` |
| heredoc body fed to a shell is over 200 lines or 8192 bytes | block, rule `unparsable`, message says to run it as a script file |
| test command targets a package that cannot be resolved | treated as "every package" → block, rule `unscoped_test` |

## Rules

Order matters: the first rule that fires decides.

| Rule | What it catches | Blocks, for example | The message tells the agent to |
|---|---|---|---|
| `unparsable` | a command the tokenizer cannot split safely; an oversized shell payload | `pnpm test "oops` | split it or quote it plainly / run it as a script file |
| `force_push` | `git push` with `--force`, `-f` in a short-flag cluster, `--mirror`, or a `+refspec`; `git` found by basename, git globals (`-C`, `-c`, `--git-dir`, …) skipped | `/usr/bin/git push --force origin main` | use `git push --force-with-lease` |
| `chained_gated` | two or more commands in one call where any of them starts with a `guard.gatedActions` prefix; payload chains count separately | `git commit -m x && git push` | run `` `git commit -m x` `` as its own call |
| `watch_mode` | bare `vitest` without `--run`; `jest --watch`/`--watchAll`; an explicit `--watch` on a test script; `test:watch`; `docker compose logs -f`; `playwright`/`test:e2e` with `--ui`, `--watch` or `--debug` | `docker compose logs -f api` | `use docker compose logs --tail=200 api` (each case gets its own one-shot form) |
| `unscoped_test` | a test runner invocation whose own arguments carry no path (`/` or `*.test.ts`-style), or one that spans every workspace package | `pnpm test` | `pnpm exec vitest --run <path/to/file.test.ts>` |
| `heavy` (warn only) | commands matching a `heavy` glob after the package-manager prefix is stripped | `pnpm build` | run it outside the agent loop or in a background task |

Runner detection covers `pnpm`/`npm`/`yarn`/`bun` scripts named `test` or `test:*`,
`run test`, `exec`/`dlx`/`npx`/`bunx` followed by `vitest` or `jest`, direct
`vitest`/`jest` binaries, `turbo run test`, `nx test`, `yarn workspace <name> test`,
`yarn workspaces foreach`, the configured `defaults.test` command, and all of these behind
env-var prefixes (`CI=1 …`) or wrappers (`sudo`, `time`, `env`, `nohup`, `command`,
`exec`, `corepack`).

`guard.mode: "warn"` downgrades every block to a `WARN` line with exit 0, for a trial
period on an existing repo. `heavy` is always a warning.

## Examples

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

## Install on an existing project (today, manual)

Requirements: `bash`, `python3` ≥ 3.9, macOS or Linux. The copied files are plain scripts
with no dependencies, so they are committed to the adopting repo and every teammate gets
them by cloning.

1. Copy the core and the Claude Code adapter so the layout is
   `.claude/hardrail/{core/, pre-bash.sh, pre_bash.py}` — the shim looks for `core/`
   beside itself first:

   ```bash
   mkdir -p .claude/hardrail
   cp -R path/to/hardrail/core .claude/hardrail/core
   cp path/to/hardrail/adapters/claude/pre-bash.sh path/to/hardrail/adapters/claude/pre_bash.py .claude/hardrail/
   chmod +x .claude/hardrail/pre-bash.sh
   ```

2. Register the hook. Merge `adapters/claude/hooks.snippet.json` into
   `.claude/settings.json` (create the file if it does not exist). The path is absolute
   via `$CLAUDE_PROJECT_DIR`, so a `cd` inside the session cannot detach the guard:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hardrail/pre-bash.sh" }
           ]
         }
       ]
     }
   }
   ```

3. Optionally add `hardrail.config.json` at the repository root (see
   [Configuration](#configuration)). Without it the defaults apply.

4. Verify from the repository root. The guard must print a `BLOCKED` line and exit 2:

   ```bash
   echo '{"tool_name":"Bash","tool_input":{"command":"pnpm test"},"cwd":"'"$PWD"'"}' \
     | bash .claude/hardrail/pre-bash.sh; echo "exit=$?"
   ```

   ```
   BLOCKED [unscoped_test]: unscoped test run; scope it: pnpm exec vitest --run <path/to/file.test.ts>
   exit=2
   ```

5. Commit `.claude/hardrail/`, `.claude/settings.json` and (if added)
   `hardrail.config.json`.

Claude Code shows the agent only stderr on exit 2, so the message is the whole feedback:
every block line ends with the command to run instead.

## Configuration

`hardrail.config.json` at the repository root; the nearest one above the command's `cwd`
is used. JSON Schema: `schema/hardrail.config.schema.json`. The file deep-merges over the
defaults: objects merge key by key, lists and scalars from the file replace the default.
An invalid file is ignored with a stderr warning and the defaults are enforced.

Keys read by the guard today (`core/config.py` `DEFAULTS`):

| Key | Type | Default | Meaning |
|---|---|---|---|
| `defaults.test` | string | `"pnpm exec vitest --run"` | the scoped test command; used in fix messages and recognised as a runner |
| `packages` | object | `{}` | `name → { root, test? }`; a package `test` command is also recognised as a runner |
| `guard.mode` | `"block"` \| `"warn"` | `"block"` | `warn` downgrades every block to a warning |
| `guard.gatedActions` | string[] | `git commit`, `git push`, `git reset`, `git rebase`, `git merge`, `git stash`, `git clean`, `rm`, `pnpm install`, `pnpm add`, `pnpm remove`, `npm install`, `yarn add`, `docker`, `gh pr`, `prisma migrate` | command prefixes that must run as their own call |
| `heavy` | string[] | `build`, `generate:*`, `prisma *`, `playwright`, `docker compose logs` | globs (`*` and `?` only) matched against the command after `pnpm`/`npm run`/`yarn`/`npx` is stripped; a match warns |
| `worktree` | object | `{ "required": false, "dir": ".worktrees", "include": [] }` | reserved for the worktree guard (not read yet) |

The schema also accepts `ci`, `codegen`, `conventionChecks`, `modules`, `language`,
`defaults.lint`, `defaults.typecheck` and `packages.*.testFor`. They belong to the
planned CI and `init` layers and are not read by any code in this release.

Example:

```json
{
  "defaults": { "test": "pnpm exec vitest --run" },
  "guard": { "mode": "block", "gatedActions": ["git commit", "git push", "rm -rf", "docker", "pnpm install", "gh pr"] },
  "heavy": ["build", "generate:*", "playwright"]
}
```

If you add a gated or heavy entry whose first word is not in the pre-filter keyword list
of `pre-bash.sh`, add the keyword there too; otherwise the bash fast path returns before
python can see it. Rendering that list from the config is on the roadmap.

## Monorepo support

`unscoped_test` needs to know which package a test command targets, because a path
argument only scopes the run inside that package. Resolution order for a
`pnpm`/`npm`/`yarn`/`bun` command:

1. `-C <dir>` / `--dir <dir>` (also the `=` forms) → that directory.
2. `--filter <name>` / `-F <name>` → the workspace package with that `name` in its
   `package.json`; `--filter ./path` → that directory; `yarn workspace <name>` the same.
3. Every `cd` earlier in the same chain is applied in order (`cd packages/web; ls; pnpm
   test src/a.test.ts` targets `packages/web`).
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
file filters and the whole `src/` directory still runs. The guard blocks and shows the direct form instead:
`pnpm -C <dir relative to cwd> exec vitest --run <path>` (just `pnpm exec vitest --run
<path>` when `cwd` is already the package).

## Latency

The hook runs on every Bash call, so it must be cheap:

- **bash fast path** — a command with no gated keyword never starts python.
- **python path** — `python3 -I -S` with a core that imports only `os`, `re`, `shlex`,
  `json`, `glob`; no `dataclasses`, `typing` or `fnmatch`, whose import cost is a large
  share of a cold start (check with `python3 -X importtime`).
- **payload cap** — a heredoc body handed to a shell is parsed only up to 200 lines /
  8192 bytes; larger bodies are blocked as `unparsable` instead of parsed.
- **CI gate** — `test/bench_guard.py` runs four cases (fast path, python path, a 20 KB
  `cat` heredoc, the cap path) 50 times each through the real hook and fails when any p95
  is 100 ms or more. It runs on `ubuntu-latest` and `macos-latest` on every push.

Local numbers depend on the machine's load and python build; a busy laptop exceeds the
gate. CI is the reference.

## Development

```bash
python3 -m unittest discover -s test -t . -v     # unit tests (78 at the time of writing)
python3 test/bench_guard.py                      # latency bench, exit 1 if any p95 >= 100 ms
python3 -m unittest test.test_no_leak            # name-leak gate over every tracked file
python3 -W error -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('core', quiet=1) and compileall.compile_dir('adapters', quiet=1) and compileall.compile_dir('test', quiet=1) else 1)"
```

The leak gate fails on ticket-id patterns, home-directory paths and e-mail addresses in any
tracked file. Set `HARDRAIL_DENYLIST=/path/to/file` (one literal per line, case-insensitive)
to also reject private names before a push; the file itself stays outside the repo. If the
variable is set and the file is missing, the gate fails rather than silently skipping.

To add a guard case, append a row to `test/core/guard_table.py`:

```python
("yarn workspace @fx/api test", "block", "unscoped_test", FX),   # 4th element: cwd relative to the repo root
```

The table test asserts both the action and the rule for every row. A new rule is a function
`(simples, config, cwd) -> Decision | None` added to `RULES` in `core/guard.py` at the
position its priority demands.

Repository layout:

```
core/            shell.py (tokenizer) · guard.py (rules, decide) · packages.py (workspace resolution)
                 config.py (defaults, loader) · argv.py (env/wrapper/option helpers)
adapters/claude/ pre-bash.sh · pre_bash.py · hooks.snippet.json
schema/          hardrail.config.schema.json
test/            core/ (table + unit tests) · adapters/ · fixtures/ · bench_guard.py · test_no_leak.py
docs/            overview.md · superpowers/specs/ · superpowers/plans/
.github/         workflows/ci.yml (unit tests + bench on ubuntu and macos)
```

## Status / roadmap

Shipped:

- [x] guard core: tokenizer with payload extraction, six rules, workspace package resolution
- [x] Claude Code adapter: `PreToolUse` hook for `Bash`, bash fast path, fail-open shim
- [x] config loader, JSON schema, defaults
- [x] CI: unit tests, latency gate, name-leak gate on ubuntu and macos

Planned, in the order of the design document's build order:

- [ ] `hardrail init` — detect the repo, write `hardrail.config.json`, copy the files, print the hooks snippet, never overwrite an existing file; `check` and `upgrade` with a content-hash manifest
- [ ] source → test mapping and the CI workflow (scoped tests from the diff, typecheck, codegen-in-sync, full suite on the base branch)
- [ ] rule templates rendered into the adopting repo (prose tier), with the pre-filter keyword list generated from the config
- [ ] worktree guard (`Edit`/`Write` on the main checkout is blocked, worktree setup printed)
- [ ] `metrics blocks` — counts of blocks, denials and compactions from local transcripts, counts only
- [ ] optional modules (cleanup, board, standup, session resume, code review, architecture test)
- [ ] adapters: `none` (git `pre-commit` + `AGENTS.md`) and `codex`; the plugin wrapper for `init | check | upgrade`

The core knows no agent: an adapter is a shim of about 30 lines that translates input and
exit codes, holds no policy, and adds no guard tests.

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
  mechanisms travel. The README states only what this repository's tests and commands
  reproduce.

## License

MIT — see `LICENSE`.
