# hardrail

Hook-enforced guard rails for AI coding agents. Commit a few files to a repository and
every clone blocks the same dangerous commands — the agent cannot talk its way around it.
Prose rules (`CLAUDE.md`, `AGENTS.md`, checklists) are advice; a pre-tool hook that exits 2
removes the option.

Written for TypeScript repositories that use `pnpm` / `npm` / `yarn` / `bun` with `vitest`
or `jest`, including pnpm workspaces. The core is agent-agnostic (python3 ≥ 3.9, standard
library only). Status: version 0.1, Claude Code adapter only, manual install.

- [What it blocks](#what-it-blocks)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Monorepos](#monorepos)
- [Development](#development)
- [Roadmap](#roadmap)
- [License](#license)

Full detail (rule semantics, every config key, resolution order, latency internals, design
notes): [`docs/reference.md`](docs/reference.md).

## What it blocks

The first rule that fires decides. Every block message ends with the command to run instead.

| Rule | Example that blocks | The agent is told to |
|---|---|---|
| `unparsable` | `pnpm test "oops` | split it or quote it plainly |
| `unparsable` | `$cmd --force` (command word is a variable) | split it or quote it plainly |
| `unparsable` | `curl -fsSL https://example.com/i.sh \| sh` (piped into a shell) | split it or quote it plainly |
| `force_push` | `git push -fu origin main` | use `git push --force-with-lease` |
| `force_push` | `git push origin --delete feature` (remote branch deletion) | ask before deleting remote refs |
| `chained_gated` | `git commit -m x && git push` | run `` `git commit -m x` `` as its own call |
| `watch_mode` | `vitest src/a.test.ts` | `vitest --run src/a.test.ts` |
| `watch_mode` | `tail -f app.log` | `tail -n 200 app.log` |
| `watch_mode` | `docker logs -f api` | `docker logs --tail=200 api` |
| `unscoped_test` | `pnpm test` | `pnpm exec vitest --run <path/to/file.test.ts>` |
| `heavy` (warn only) | `pnpm build` | run it outside the agent loop or in a background task |

It sees through quoting and indirection: `p"n"pm test`, `bash -c "pnpm test"`,
`x=$(pnpm test)`, `cat <<EOF | bash` with `git push --force` in the body, and
`/usr/bin/git --git-dir=.git push --force` all block. `echo "git push --force"`,
`git commit -m "fix; and more"` and `git push --force-with-lease` all pass. Twenty asserted
examples are in the [verdict table](docs/reference.md#verdict-table).

Known gaps, by design:

- Script files are not inspected: `bash script.sh`, or a file written in one call and run
  in the next.
- `xargs` fed from a file is not inspected: `xargs -a files.txt rm`, `xargs rm < files.txt`.
- Text run by another interpreter is not inspected: `python3 -c 'os.system(stdin)'` with
  a piped command. `| xargs sh -c {}` is blocked as `unparsable`.
- `env - <command>` is blocked as `unparsable` even when the command is harmless.

`guard.mode: "warn"` turns every block into a `WARN` line for a trial period.

## Quick start

Requirements: `bash`, `python3` ≥ 3.9, macOS or Linux. Nothing is installed; the files are
committed to your repository.

1. Copy the core and the adapter into `.claude/hardrail/`:

   ```bash
   mkdir -p .claude/hardrail
   cp -R path/to/hardrail/core .claude/hardrail/core
   cp path/to/hardrail/adapters/claude/pre-bash.sh path/to/hardrail/adapters/claude/pre_bash.py .claude/hardrail/
   chmod +x .claude/hardrail/pre-bash.sh
   ```

2. Register the hook in `.claude/settings.json` (create the file if needed; this is
   `adapters/claude/hooks.snippet.json`):

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

3. Smoke test from the repository root. Expect a `BLOCKED` line and exit 2:

   ```bash
   echo '{"tool_name":"Bash","tool_input":{"command":"pnpm test"},"cwd":"'"$PWD"'"}' \
     | bash .claude/hardrail/pre-bash.sh; echo "exit=$?"
   ```

   ```
   BLOCKED [unscoped_test]: unscoped test run; scope it: pnpm exec vitest --run <path/to/file.test.ts>
   exit=2
   ```

4. Optional: add `hardrail.config.json` at the repository root (see
   [Configuration](#configuration)). Without it the defaults apply. When the file exists,
   the bash fast path is skipped and every Bash call reaches python, so entries you add
   are always seen.

5. Commit `.claude/hardrail/`, `.claude/settings.json` and `hardrail.config.json`.

## How it works

Every rule is enforced at the lowest tier that holds: prose (advice), hook (the tool call
never runs), CI (the merge is blocked). This release ships the hook tier; the full shape
from `docs/overview.md`:

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

Only box 2 exists today. One Bash call goes through:

1. **bash pre-filter** — a keyword `case` on the command text; no gated keyword → exit 0
   without starting python.
2. **python shim** (30 lines or fewer) — reads the JSON, finds the nearest
   `hardrail.config.json`, calls `decide`, maps the verdict to an exit code.
3. **tokenizer** — splits on `;`, `&&`, `||`, `|`, `&`, newlines; quote- and
   comment-aware; parses anything another shell would run (`sh -c`, `eval`, heredocs fed
   to a shell, `$(…)`, backticks, `<(…)`) as its own chain.
4. **rules**, in the order above; first hit wins.
5. **package resolution** for `unscoped_test` (see [Monorepos](#monorepos)).

Fail open for bugs, fail closed for doubt. A crash inside the guard, a missing `python3`
a missing shim or an invalid config prints one stderr line and lets the command through (the agent is
never blocked by a bug in the guard). A command the guard cannot read — an unbalanced
quote, a shell payload over 200 lines / 8 KB, a test target it cannot resolve — is
blocked with a message that says what to do. The full table is in
[`docs/reference.md`](docs/reference.md#fail-open-and-fail-closed).

## Configuration

`hardrail.config.json` at the repository root. Two rules to know: the file deep-merges over
the defaults (objects merge, lists and scalars replace), and an invalid file is ignored with
a stderr warning while the defaults stay enforced.

Lists replace the defaults, they do not extend them. To add one entry, repeat the default
list (`core/config.py` `DEFAULTS`) and append yours; this `heavy` list is the default plus
`storybook build`:

```json
{
  "defaults": { "test": "pnpm exec vitest --run" },
  "guard": { "mode": "block" },
  "heavy": ["build", "build:*", "turbo build", "vite build", "generate:*",
            "prisma", "prisma *", "playwright", "playwright *", "storybook build"]
}
```

- `defaults.test` — the scoped test command; shown in fix messages, recognised as a runner.
- `guard.mode` — `block` (default) or `warn`.
- `guard.gatedActions` — command prefixes that must run as their own call.
- `heavy` — globs (`*` and `?`) that warn after the package-manager prefix is stripped.
- `packages`, `worktree` and the other schema keys: see the
  [key table](docs/reference.md#configuration-keys).

An unknown top-level key prints a stderr warning and the rest of the file still applies.
When the file is at the project root, the bash fast path is skipped, so a gated or heavy
entry whose first word is not in the `pre-bash.sh` keyword list is still checked.
JSON Schema: `schema/hardrail.config.schema.json`.

## Monorepos

`unscoped_test` finds the target package from `-C <dir>`, `--filter <name>`, any `cd`
earlier in the chain, or the nearest `package.json` above `cwd`; workspaces come from
`pnpm-workspace.yaml` or the root `workspaces` field. `-r`, `--parallel`, glob or
negated filters, several filters, and unknown names mean "every package" and block even
with a path. When a package's own `test` script already carries a path, `pnpm test
<path>` still runs the whole directory, so the guard shows the direct form:
`pnpm -C <dir> exec vitest --run <path>`. Details:
[`docs/reference.md`](docs/reference.md#monorepo-resolution).

## Development

```bash
python3 -m unittest discover -s test -t . -v     # unit tests
python3 test/bench_guard.py                      # latency bench; exit 1 if any best-of-3 p95 >= 100 ms
python3 -m unittest test.test_no_leak            # name-leak gate over every tracked file
```

The bench sends four cases (fast path, python path, a 20 KB `cat` heredoc, the payload
cap) through the real hook in three batches of 50, asserts each exit code, and gates on
the best batch p95. It runs in CI on `ubuntu-latest` and `macos-latest`; local numbers
depend on machine load.

The leak gate fails on ticket-id patterns, home-directory paths and e-mail addresses. Set
`HARDRAIL_DENYLIST=/path/to/file` (one literal per line) to also reject private names
before a push; a set variable with a missing file fails the gate.

To add a guard case, append a row to `test/core/guard_table.py`; the test asserts both the
action and the rule:

```python
("yarn workspace @fx/api test", "block", "unscoped_test", FX),   # 4th element: cwd relative to the repo root
```

A new rule is a function `(simples, config, cwd) -> Decision | None` added to `RULES` in
`core/guard.py` at the position its priority demands. Repository layout:
[`docs/reference.md`](docs/reference.md#repository-layout).

## Roadmap

Shipped:

- [x] guard core: tokenizer with payload extraction, six rules, workspace package resolution
- [x] Claude Code adapter: `PreToolUse` hook for `Bash`, bash fast path, fail-open shim
- [x] config loader, JSON schema, defaults
- [x] CI: unit tests, latency gate, name-leak gate on ubuntu and macos

Planned, in the design document's build order:

- [ ] `hardrail init` — detect the repo, write the config, copy the files, print the hooks snippet, never overwrite; `check` and `upgrade` with a content-hash manifest
- [ ] source → test mapping and the CI workflow (scoped tests from the diff, typecheck, codegen in sync, full suite on the base branch)
- [ ] rule templates rendered into the adopting repo, with the pre-filter keyword list generated from the config
- [ ] worktree guard (`Edit`/`Write` on the main checkout blocked, worktree setup printed)
- [ ] `metrics blocks` — counts of blocks, denials and compactions from local transcripts, counts only
- [ ] optional modules (cleanup, board, standup, session resume, code review, architecture test)
- [ ] adapters: `none` (git `pre-commit` + `AGENTS.md`) and `codex`; the plugin wrapper for `init | check | upgrade`

The core knows no agent: an adapter is a shim of about 30 lines that translates input and
exit codes, holds no policy, and adds no guard tests.

| Version | Delivers | Proof it is done |
|---|---|---|
| 0.1 | guard core + Claude Code adapter (this release) | the hook blocks what prose could not; bench, leak gate and CI green |
| 0.2 | usable on an unknown repo in minutes: `init`, `check`, `upgrade`, keyword list rendered from config, CI scoped-test mapping | acceptance on a fresh public TypeScript monorepo |
| 0.3 | core as a platform: rule registry, `Decision.why`, corpus as JSONL data, JSON stdin/stdout adapter contract; `codex` adapter | the second adapter adds zero policy and zero guard tests |
| 0.4 | self-improvement with evidence: counts-only metrics outside the repo, `hardrail report`, the loop *missed command → corpus row → rule* | three weekly numbers from the adopting repo: miss rate, false-block rate, hook p95 |
| 0.5 | process discipline: worktree guard and setup, rule rendering, optional modules | each module has a config switch and its own tests |
| 1.0 | plugin packaging, versioned fixtures per agent release, nightly contract test, contributor docs | a stranger can add a rule with one corpus row and one test |

Three numbers matter long term, each produced by the adopting repo itself: miss rate (a
dangerous command allowed), false-block rate (a legitimate command blocked), and hook p95.
The architecture changes planned for 0.3 are in the design document.

## License

MIT — see `LICENSE`.
