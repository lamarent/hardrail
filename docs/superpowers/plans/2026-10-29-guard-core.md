# Guard Core + Claude Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `bash-guard` that decides allow / warn / block for a shell command from `hardrail.config.json`, exposed to Claude Code as a PreToolUse hook, with a 40-row table test and a latency benchmark in this repo's CI.

**Architecture:** `core/` is agent-agnostic python3 stdlib: `shell.py` tokenises a command into simple commands and payloads, `config.py` loads and defaults the config, `packages.py` resolves which workspace package a command targets, `guard.py` applies the rules and returns a `Decision`. `adapters/claude/` is a ≤30-line shim: a bash fast path that exits 0 when no gated keyword appears, then a python entry that maps Claude's stdin JSON to `decide()` and its result to exit 2 + stderr.

**Tech Stack:** python3 ≥ 3.9 stdlib (`shlex`, `json`, `re`, `unittest`), bash, GitHub Actions. No third-party packages anywhere.

**Spec:** `docs/superpowers/specs/2026-10-29-hardrail-design.md` — sections "Core and adapters", "2. Hooks", "Verification" rows bash-guard and latency.

## Global Constraints

- Hooks: python3 stdlib only, self-contained, no `jq`, no Node, no import from outside the copied directory.
- Hook p95 latency < 100 ms, cold and warm, measured in CI.
- Unparsable input: block in `block` mode, warn in `warn` mode, never silent allow.
- Corrected command goes to **stderr** (Claude reads only stderr on exit 2).
- Fail open with one stderr line when python3 is missing (the bash shim handles this).
- No repository, company, product, ticket or person name anywhere, including test fixtures and commit messages.
- Commit after every task; messages in English.

## Review Focus

1. `CI=1 pnpm test src/a.test.ts` — env-var prefix before the runner must still be recognised as a test run and allowed. Test in Task 3.
2. `pnpm -r test` / `pnpm --filter '*' test` — recursive or glob filter means every package; must block as unscoped even with a path. Test in Task 4.
3. `pnpm test # src/` and `git log -- src/; pnpm test` — a path in a comment or in another simple command must not count as scope. Test in Task 3.
4. A command of 20 KB (pasted heredoc) — must return within budget and not raise; `shlex` on huge input is linear, verify in Task 6 bench with a long-input row.
5. Non-ASCII in quotes (`git commit -m "修正"`) alone, not chained — must allow. Test in Task 1.

---

### Task 1: Shell tokeniser

**Files:**
- Create: `core/__init__.py` (empty), `core/shell.py`
- Test: `test/core/test_shell.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class Simple:          # one simple command
      argv: list[str]
      raw: str           # original text slice, for messages
      payload_of: str | None   # "sh -c" | "eval" | "heredoc" when this came from a payload

  class ParseError(Exception): ...

  def split_commands(cmd: str) -> list[Simple]
  ```
  Separators: `;` `&` `&&` `||` `|` and newline. Payloads of `bash|sh|zsh [-l] -c '…'`, `eval '…'`, and a heredoc whose reader is `bash|sh|zsh` are recursively split and appended with `payload_of` set. Heredoc bodies with any other reader are dropped. `shlex` with `posix=True`; `ValueError` from shlex → `ParseError`. `\;` inside `find … -exec … \;` is not a separator.

- [ ] **Step 1: Write the failing tests**

```python
class SplitTests(unittest.TestCase):
    def argvs(self, cmd): return [s.argv for s in split_commands(cmd)]
    def test_single(self): self.assertEqual(self.argvs('pnpm test'), [['pnpm','test']])
    def test_quote_splice(self): self.assertEqual(self.argvs('p"n"pm test'), [['pnpm','test']])
    def test_escaped_quote_not_eaten(self):
        self.assertEqual(self.argvs('echo "\\"" ; pnpm test ; echo "\\""')[1], ['pnpm','test'])
    def test_newline_and_ampersand(self):
        self.assertEqual(self.argvs('git commit -m x\ngit push'), [['git','commit','-m','x'],['git','push']])
        self.assertEqual(self.argvs('git commit -m x & git push'), [['git','commit','-m','x'],['git','push']])
    def test_sh_c_payload(self):
        s = split_commands('bash -c "pnpm test"'); self.assertEqual(s[-1].argv, ['pnpm','test']); self.assertEqual(s[-1].payload_of, 'sh -c')
    def test_heredoc_into_bash(self):
        s = split_commands('bash <<EOF\npnpm test\nEOF'); self.assertEqual(s[-1].argv, ['pnpm','test']); self.assertEqual(s[-1].payload_of, 'heredoc')
    def test_heredoc_into_cat_dropped(self):
        self.assertEqual(self.argvs('cat <<EOF\npnpm test\nEOF'), [['cat']])
    def test_find_exec_semicolon(self):
        self.assertEqual(len(split_commands('find . -name "*.orig" -exec rm {} \\;')), 1)
    def test_docker_sh_c_payload_separate(self):
        s = split_commands('docker run --rm alpine sh -c "ls; pwd"')
        self.assertEqual(s[0].argv[:2], ['docker','run']); self.assertEqual([x.argv for x in s[1:]], [['ls'],['pwd']])
    def test_non_ascii(self): self.assertEqual(self.argvs('git commit -m "修正"'), [['git','commit','-m','修正']])
    def test_unbalanced_raises(self):
        with self.assertRaises(ParseError): split_commands('pnpm test "oops')
```

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test.core.test_shell -v` → ImportError.
- [ ] **Step 3: Implement `split_commands` in `core/shell.py`.** Approach: strip heredoc bodies first with a regex that records reader argv and body; tokenise the remainder with `shlex.shlex(punctuation_chars=';&|')`; group tokens into simple commands on the punctuation tokens and on newline; after grouping, scan each simple command for `sh -c` / `eval` payloads and recurse.
- [ ] **Step 4: Run to verify pass** — same command, `OK`.
- [ ] **Step 5: Commit** — `git add core test && git commit -m "feat(core): shell tokeniser with payload extraction"` (one command, no chain: run `git add` then `git commit` as two Bash calls).

### Task 2: Config loader

**Files:**
- Create: `core/config.py`, `schema/hardrail.config.schema.json`, `test/fixtures/config/minimal.json`, `test/fixtures/config/full.json`
- Test: `test/core/test_config.py`

**Interfaces:**
- Produces:
  ```python
  DEFAULTS = {
    "defaults": {"test": "pnpm exec vitest --run"},
    "packages": {},
    "guard": {"mode": "block",
              "gatedActions": ["git commit","git push","git reset","git rebase","git merge","git stash","git clean",
                               "rm","pnpm install","pnpm add","pnpm remove","npm install","yarn add",
                               "docker","gh pr","prisma migrate"]},
    "heavy": ["build","generate:*","prisma *","playwright","docker compose logs"],
    "worktree": {"required": False, "dir": ".worktrees", "include": []},
  }
  def load(repo_root: str) -> dict        # reads <root>/hardrail.config.json, deep-merges over DEFAULTS
  def find_root(cwd: str) -> str | None   # nearest ancestor containing hardrail.config.json
  ```
  `full.json` is the spec's sample config verbatim. `minimal.json` is `{}`.

- [ ] **Step 1: Write the failing tests** — `test_minimal_gets_defaults` (mode == "block", gatedActions contains "git push"), `test_full_overrides` (packages has "backend", guard.mode from file), `test_find_root_walks_up` (tmp dir with config two levels above cwd), `test_find_root_none`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `load`, `find_root` and write the JSON schema** (draft-07, `additionalProperties: false` at top level, every key in the spec's sample optional).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `feat(core): config loader with defaults and schema`.

### Task 3: Guard decision

**Files:**
- Create: `core/guard.py`
- Test: `test/core/test_guard.py`, `test/core/guard_table.py`

**Interfaces:**
- Consumes: `split_commands`, `ParseError` (Task 1); `load` (Task 2); `resolve_package` (Task 4 — stub returning `None` until Task 4 lands).
- Produces:
  ```python
  @dataclass
  class Decision:
      action: str          # "allow" | "warn" | "block"
      rule: str            # "force_push" | "chained_gated" | "watch_mode" | "unscoped_test" | "heavy" | "unparsable" | ""
      message: str         # one line, ends with the corrected command when one exists
  def decide(command: str, cwd: str, config: dict) -> Decision
  ```
  Rules, in order, first hit wins:
  1. `unparsable` — `ParseError` → block/warn per `guard.mode`, message "cannot parse command; split it or quote it plainly".
  2. `force_push` — any simple `git [globals] push …` with `--force`, a short-flag cluster containing `f` (not `--force-with-lease`), or a `+refspec` arg.
  3. `chained_gated` — more than one simple command, and any of them starts with a `gatedActions` prefix (after git globals `-C x` / `-c k=v`). Payload commands count as their own chain only.
  4. `watch_mode` — a test runner simple command with no `--run` / `run` / `--watch=false` / `--no-watch` and no `CI=` prefix; or `test:watch`; or `docker compose logs -f/--follow`; or `playwright|test:e2e` with `--ui|--watch|--debug`.
  5. `unscoped_test` — a test runner simple command whose **own argv** has no path argument (a token containing `/` or ending `.test.|.spec.` + `ts|tsx|js|jsx`) and is not `--help`. Runner detection: argv[0] in `pnpm|npm|yarn|bun|npx` with a `test`/`test:*`/`run test` token, or argv[0] basename in `vitest|jest`, or `turbo run test`, `nx test`, `node_modules/.bin/vitest|jest`, or argv[0] equal to the first token of any configured `test` command. Env-var prefixes (`A=b`) are skipped before argv[0].
  6. `heavy` — argv matches a `heavy` glob after stripping `pnpm|npm run|yarn|npx` prefix → **warn** only ("dispatch to a runner agent").
  Message for `unscoped_test` is `"<fix>"` where fix = `<packageTest> <firstPathFromCommandOrPlaceholder>`; when Task 4 reports the package `test` script already carries a path, the fix uses `pnpm -C <root> exec vitest --run <path>` instead.

- [ ] **Step 1: Write `guard_table.py`** — `ROWS: list[tuple[str, str, str]]` of `(command, expected_action, expected_rule)` with ≥ 40 rows, including every Review Focus item and every bypass from the spec: `pnpm test`→block/unscoped_test; `p"n"pm test`→block; `npm test`, `bun test`, `npx jest`, `turbo run test`, `./node_modules/.bin/vitest`→block; `pnpm test # src/`→block; `git log -- src/; pnpm test`→block; `pnpm exec vitest --run src/a.test.ts`→allow; `pnpm exec vitest --run tests/unit/`→allow; `pnpm exec vitest --watch=false src/a.test.ts`→allow; `CI=1 pnpm test src/a.test.ts`→allow; `vitest src/a.test.ts`→block/watch_mode; `pnpm test:watch`→block/watch_mode; `git commit -m x && git push`→block/chained_gated; `git commit -m x\ngit push`→block; `git commit -m x & git push`→block; `git push --force`→block/force_push; `git push -fu origin main`→block; `git -C repo push --force origin main`→block; `git push --force-with-lease`→allow; `git push origin +main`→block; `bash -c "pnpm test"`→block; `bash <<EOF\npnpm test\nEOF`→block; `find . -name "*.orig" -exec rm {} \;`→allow; `docker run --rm alpine sh -c "ls; pwd"`→allow; `git commit -m "fix; and more"`→allow; `pnpm build`→warn/heavy; `docker compose logs -f api`→block/watch_mode; `docker compose logs --tail=100 api`→allow; `pnpm test "oops`→block/unparsable; `ls; pwd`→allow; `git status && git diff`→allow; `echo "git push --force"`→allow; `pnpm exec vitest --run src/a.test.ts | tail -5`→allow; `git commit -m "修正"`→allow.
- [ ] **Step 2: Write `test_guard.py`** — `test_table` iterates `ROWS` with `subTest`, asserting `(d.action, d.rule)`; `test_warn_mode_downgrades_block` (config mode warn → `pnpm test` gives warn/unscoped_test); `test_message_has_fix` (`pnpm test` message contains `pnpm exec vitest --run`); `test_message_on_single_line`.
- [ ] **Step 3: Run to verify failure.**
- [ ] **Step 4: Implement `decide` in `core/guard.py`** with one small function per rule, each `(simples, config) -> Decision | None`.
- [ ] **Step 5: Run to verify pass** — `python3 -m unittest test.core.test_guard -v` → 40+ subtests OK.
- [ ] **Step 6: Commit** — `feat(core): guard decision with 40-row table test`.

### Task 4: Package resolution

**Files:**
- Create: `core/packages.py`, `test/fixtures/pnpm-monorepo/` (root `package.json` with `workspaces`, `pnpm-workspace.yaml` listing `packages/*`, `packages/api/package.json` `{name:"@fx/api", scripts:{test:"vitest"}}`, `packages/web/package.json` `{name:"@fx/web", scripts:{test:"vitest src/"}}`)
- Modify: `core/guard.py` (replace the stub)
- Test: `test/core/test_packages.py`, add rows to `guard_table.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class Package:
      name: str; root: str; test_script: str | None
  def resolve_package(simple: Simple, cwd: str, config: dict, repo_root: str) -> Package | None | str
  ```
  Returns `"all"` for `-r`, `--recursive`, `--filter` with a glob/selector (`*`, `...`, `[`), or `--parallel`; a `Package` from `-C <dir>`, `--filter <name>` (name → root via each workspace `package.json`), a leading `cd <dir>` in the same simple command's raw text, else the nearest ancestor of `cwd` with a `package.json`; `None` when nothing found. `~` and `$VAR` are expanded with `os.path.expanduser` / `expandvars`. Minimal `pnpm-workspace.yaml` parser: lines under `packages:` starting with `- `.

- [ ] **Step 1: Write the failing tests** — `test_dash_C`, `test_filter_name`, `test_filter_glob_is_all`, `test_recursive_is_all`, `test_cwd_ancestor`, `test_cd_prefix_word_boundary` (`abcd foo; pnpm test` does not resolve `foo`), `test_script_with_path_detected` (`@fx/web` → `test_script == "vitest src/"`).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `resolve_package`; wire into `decide`**: `"all"` → `unscoped_test` even with a path; a package whose `test_script` contains a path token → fix message `pnpm -C <root> exec vitest --run <path>`.
- [ ] **Step 4: Add table rows** — `pnpm -r test`→block; `pnpm --filter '*' test src/a.test.ts`→block; `pnpm --filter @fx/api test src/a.test.ts`→allow; `pnpm -C packages/web test src/a.test.ts`→block with message containing `-C`; run with `cwd=test/fixtures/pnpm-monorepo`.
- [ ] **Step 5: Run to verify pass** — `python3 -m unittest discover -s test -v`.
- [ ] **Step 6: Commit** — `feat(core): workspace package resolution for test scoping`.

### Task 5: Claude adapter

**Files:**
- Create: `adapters/claude/pre-bash.sh`, `adapters/claude/pre_bash.py`, `adapters/claude/hooks.snippet.json`
- Test: `test/adapters/test_claude.py`

**Interfaces:**
- Consumes: `decide`, `find_root`, `load`.
- Produces: `pre-bash.sh` reads stdin to a temp var, `case` on the raw text for `test|vitest|jest|push|commit|rm|docker|prisma|generate|install|build|playwright|logs` → exit 0 if none; else `exec python3 -I -S "$(dirname "$0")/pre_bash.py"`; if `python3` is missing print `hardrail: python3 not found, guard disabled` to stderr and exit 0. `pre_bash.py` (≤ 30 lines): parse `tool_input.command` and `cwd`, `root = find_root(cwd)`, `config = load(root) if root else DEFAULTS`, `d = decide(...)`; `block` → stderr `BLOCKED [<rule>]: <message>`, exit 2; `warn` → stderr `WARN [<rule>]: <message>`, exit 0; else exit 0. `hooks.snippet.json` is the `hooks.PreToolUse` entry with matcher `Bash` and command `.claude/hardrail/pre-bash.sh`.

- [ ] **Step 1: Write the failing tests** (subprocess, `cwd=test/fixtures/pnpm-monorepo`): `test_block_exit_2_stderr` (`pnpm test` → rc 2, stderr startswith `BLOCKED [unscoped_test]`, stdout empty); `test_allow_exit_0` (`git status`); `test_fast_path_skips_python` (`ls -la` with `PATH` pointing to a dir without python3 → rc 0, no stderr); `test_missing_python_fails_open` (`pnpm test` with that PATH → rc 0, stderr contains `guard disabled`); `test_warn_exit_0` (`pnpm build` → rc 0, stderr startswith `WARN [heavy]`).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement the two files and the snippet.** `pre_bash.py` adds its own parent's parent (`core/` sibling) to `sys.path` only when copied layout is `.claude/hardrail/{core,pre_bash.py}`; document the copied layout in a comment.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `feat(adapters): claude PreToolUse shim with bash fast path`.

### Task 6: Latency benchmark and repo CI

**Files:**
- Create: `test/bench_guard.py`, `.github/workflows/ci.yml`, `test/__init__.py`, `test/core/__init__.py`, `test/adapters/__init__.py`
- Test: the workflow itself

**Interfaces:**
- Produces: `bench_guard.py` runs `adapters/claude/pre-bash.sh` 50× each for `git status` (fast path), `pnpm exec vitest --run src/a.test.ts` (python path), and a 20 KB heredoc command; prints p50/p95 per case; exits 1 if any p95 ≥ 100 ms. `ci.yml`: `on: [push, pull_request]`, matrix `ubuntu-latest` + `macos-latest`, steps: checkout (pinned SHA), `python3 -m unittest discover -s test -v`, `python3 test/bench_guard.py`, `permissions: contents: read`.

- [ ] **Step 1: Write `bench_guard.py`** with the three cases and the exit rule.
- [ ] **Step 2: Run locally** — `python3 test/bench_guard.py` → three lines, exit 0. If p95 ≥ 100 ms on the python path, move the `import` of `core.packages` behind the runner check in `decide` and re-run.
- [ ] **Step 3: Write `ci.yml`.**
- [ ] **Step 4: Verify** — `actionlint .github/workflows/ci.yml` (if installed) → no output; `python3 -m unittest discover -s test` → OK.
- [ ] **Step 5: Commit** — `ci: unit tests and guard latency benchmark on ubuntu and macos`.

### Task 7: Name-leak gate

**Files:**
- Create: `test/test_no_leak.py`
- Test: itself

- [ ] **Step 1: Write the test** — walks every tracked file (`git ls-files`), fails on regex `[A-Z]{2,6}[0-9]*-[0-9]{2,}` (ticket ids), `/Users/`, `/home/`, `[\w.]+@[\w.]+\.\w+`, and any line matching `_DENY` read from env `HARDRAIL_DENYLIST` (a local file path, absent in CI).
- [ ] **Step 2: Run** — `python3 -m unittest test.test_no_leak` → OK. If it fails, fix the file, never the regex.
- [ ] **Step 3: Commit** — `test: name-leak gate over tracked files`.

---

## Self-review notes

- Spec coverage for this plan's slice: shell parsing, every bypass and false positive in the safety review, package resolution, warn mode, fail-open, stderr contract, bench, leak gate — covered. `worktree-guard`, `init`, CI mapping, render, metrics: separate plans.
- Names used across tasks: `Simple`, `ParseError`, `split_commands`, `Decision`, `decide`, `load`, `find_root`, `DEFAULTS`, `Package`, `resolve_package` — consistent.
- Review Focus items 1, 3, 5 → Task 3 table; 2 → Task 4 rows; 4 → Task 6 bench.
