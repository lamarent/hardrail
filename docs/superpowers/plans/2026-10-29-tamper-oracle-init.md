# Tamper Resistance, Bash Oracle, `init` and Release Hygiene (Plan 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent can no longer switch the guard off by editing its files; every guard-table row is checked against what real bash would execute; `hardrail init` installs and verifies the copied layout with a manifest, rendering the bash pre-filter from the config so custom gated actions are never silently skipped; and the repo ships what a public project needs to be trusted and contributed to: ADRs, a changelog, a config schema, a contributing guide and a second adapter.

**Architecture:** A second core decision, `decide_write(path, cwd, config)` in `core/files.py`, guards file writes; the same protected-path set is enforced inside `bash-guard` by a new rule `protected_path` (redirect targets and arguments of mutating commands). The Claude adapter gains a second ≤ 30-line shim for `Edit|Write|MultiEdit|NotebookEdit`. `test/oracle/` runs table rows through real `bash` with fake binaries on `PATH` that log their argv, and asserts that no row the guard allows would have executed a gated or unscoped invocation. `bin/hardrail` (python3, stdlib) implements `init` and `init --check` per the spec (`--upgrade` deferred, see Ruling 11); the pre-filter keyword `case` is generated from the config between marker lines.

**Tech Stack:** python3 ≥ 3.9 stdlib, bash, GitHub Actions. Depends on Plan 1.5 (`docs/superpowers/plans/2026-10-29-hardening.md`) being merged first: `Simple.sep`, `_drop_redirects`, `strip_env_and_wrappers` raising `ParseError`, `_heavy_form`.

**Spec:** `docs/superpowers/specs/2026-10-29-hardrail-design.md` — "5. `init`", "Manifest" row, "Claude Code compatibility measures", Verification rows "init no-overwrite", "init --check", "acceptance"; hooks table row `worktree-guard` (PreToolUse `Edit|Write`) establishes the write-hook shape this plan reuses.

## Global Constraints

- python3 ≥ 3.9 stdlib only; no `dataclasses`/`typing`/`fnmatch` in `core/`; `bin/hardrail` may import `hashlib`, `argparse`, `shutil`.
- Each adapter shim ≤ 30 lines, no policy; fail open with one stderr line only on the shim's own errors.
- `init` never modifies a file the repo already had (spec §5): existing files are left in place and reported.
- **Lean:** no task adds a CI job, a tool, a dashboard or a file that a one-line note elsewhere would replace. Docs files have a line cap stated in their task. A task that grows past its brief stops and reports instead of expanding.
- `init` prints the hooks snippet and does **not** edit `.claude/settings.json`.
- Hook p95 < 100 ms on CI for the bash hook; the write hook must be ≤ 50 ms p95 (it has no tokenizer).
- No repository, company, product, ticket or person name anywhere; `test/test_no_leak.py` must pass.
- Oracle tests run only real `bash` with fake binaries from `test/oracle/bin`; nothing in a row may reach a real `git`, `rm`, `pnpm`, `docker`, `curl`, `sudo`: the test sets `PATH` to the fake dir plus `/usr/bin:/bin` and asserts the fake dir shadows every name in `test/oracle/bin`.
- Commit after every task; English messages ending with the session's attribution trailer.

## Rulings

1. Protected paths default: `.claude/hardrail/**`, `.claude/.hardrail-manifest.json`, `hardrail.config.json`, `.claude/settings.json`, `.claude/settings.local.json`. Config key `guard.protectedPaths` (list, replace semantics). Globs use `_glob_match` semantics (`*` spans anything incl. `/`, `?` one char).
2. In bash, a protected path is blocked when it is a redirect target, or an argument of a command **not** in the read-only allowlist `cat less more head tail grep rg diff ls stat wc file md5sum shasum sha256sum jq bat` or `git diff|status|log|show|blame` or `python3 -m json.tool`. Everything else (including `rm`, `mv`, `cp`, `sed`, `tee`, `chmod`, `ln`, `truncate`, `git checkout|restore|rm|stash`, `perl`, `python3 -c`) blocks. Fail closed beats a mutating-command list.
3. Rule order: `unparsable → force_push → protected_path → chained_gated → watch_mode → unscoped_test → heavy`.
4. The write shim resolves `tool_input.file_path` (or `notebook_path`) against `cwd`; paths outside the project root are allowed (not ours to guard). Relative project root = nearest ancestor with `hardrail.config.json`, else `$CLAUDE_PROJECT_DIR` passed in by the shim, else `cwd`.
5. Oracle verdicts: a row is a **bypass** when the guard allows (or warns) and the fake log shows a dangerous invocation; a row is a **false block** when the guard blocks and the log shows none. Bypass fails the test. False blocks are collected and asserted against an allowlist file `test/oracle/known_false_blocks.txt` (one row text per line), so new false blocks fail the test and known ones are visible.
6. "Dangerous invocation" in the oracle log covers only the three rules whose bypass is irreversible: `git push` with `-f`/`--force`/`--mirror`/`--delete`/`-d`/a `+ref` or `:ref` arg (force_push); a gated prefix from `DEFAULTS.guard.gatedActions` appearing together with any other logged invocation (chained_gated); any write to a protected path (fake binaries log `PWD` and args; the test checks args against the protected globs) (protected_path). Rows whose expected rule is `watch_mode`, `unscoped_test` or `heavy` are run but only checked for bypass of those three. This matcher is independent of `core/shell.py`; it may share nothing with it.
7. Rows the oracle skips: expected `unparsable` (bash itself rejects most), rows longer than 8 KB, rows that need the fixture cwd use a tempdir copy of `test/fixtures/pnpm-monorepo`.
8. `init` layout: `.claude/hardrail/{core/, pre-bash.sh, pre_bash.py, pre-write.sh, pre_write.py}` and `.claude/.hardrail-manifest.json` (`{"version": "<core version>", "files": {"<rel path>": "sha256:…"}}`). `core/__init__.py` gets `__version__ = "0.2.0"`.
9. Pre-filter rendering: `pre-bash.sh` carries `# hardrail:keywords:start` / `# hardrail:keywords:end` markers around the `case` patterns; `init` and `--upgrade` rewrite that block from the config's `guard.gatedActions`, `heavy`, `defaults.test` and `packages.*.test` first words plus the fixed runner/shell words (`test vitest jest npx bun yarn npm pnpm turbo bash sh zsh dash ksh eval tail`). The checked-in `pre-bash.sh` is rendered from `DEFAULTS`; `test_claude.py` asserts the file equals a fresh render of `DEFAULTS`. The Plan 1.5 "skip fast path when a config exists" stays as a belt-and-braces fallback for a stale render; `init --check` reports a stale render.
10. `init --check` exit codes: 0 ok, 1 problems found (each printed as `path or hook: reason`), 2 usage. `claude --version` comparison from the spec is deferred (no `minVersion` data yet); `--check` prints the version if `claude` is on `PATH`.
11. `init --upgrade` is deferred to a later release: the manifest stays (it is what `--check` uses to report `edited <path>`), but no upgrade logic, no `.hardrail-new` files. Re-running `init` after deleting `.claude/hardrail/` is the documented upgrade path.
12. ADRs: `docs/adr/NNNN-<slug>.md`, MADR-style, sections `Status / Context / Decision / Consequences`, ≤ 25 lines each, at most 7 in this plan. An ADR records a decision that would be costly to reverse (enforcement model, language, layout, failure policy, parser, rule order, pre-filter). Small rulings (an alias, a table row) go to the changelog, never an ADR. Contributors add or update an ADR in the same PR when they change rule order, exit codes, config keys or an intentional non-block; CONTRIBUTING says so in one line and there is no CI check for it.
13. Process: Tasks 5 and 6 (docs, adapter) are reviewed in the final whole-branch review, not per task; Tasks 1–4 keep per-task review because they touch `core/`.

## Review Focus

1. `echo x > hardrail.config.json` and `sed -i 's/block/warn/' hardrail.config.json` — must block `protected_path`. Task 1.
2. `Write` with `file_path` `.claude/hardrail/pre_bash.py` from a nested cwd — must block via the write hook. Task 2.
3. A guard-table row the guard allows whose fake log shows `git push --force` — oracle must fail loudly with the row text. Task 3.
4. `init` run twice on a repo that already has `.claude/settings.json` — second run writes nothing, settings untouched (hash before/after). Task 4.
5. The second adapter fed the same command that blocks under Claude — must block with the same rule id and the agent-specific deny signal. Task 6.

---

### Task 1: `protected_path` in core (files + bash rule)

**Files:**
- Create: `core/files.py`
- Modify: `core/config.py` (`DEFAULTS["guard"]["protectedPaths"]`), `core/guard.py` (new rule, `RULES` order), `schema/hardrail.config.schema.json`
- Test: `test/core/test_files.py` (new), `test/core/guard_table.py`, `test/core/test_config.py`

**Interfaces:**
- `core/files.py`:
  ```python
  PROTECTED_DEFAULT = ['.claude/hardrail/**', '.claude/.hardrail-manifest.json', 'hardrail.config.json', '.claude/settings.json', '.claude/settings.local.json']
  def project_root(cwd, hint=None) -> str      # nearest ancestor with hardrail.config.json, else hint, else cwd
  def is_protected(path, root, config) -> bool  # path absolute or relative to root; normalised; matched with glob semantics against config['guard']['protectedPaths']; `**` matches any depth
  def decide_write(path, cwd, config, hint=None) -> Decision  # block, rule 'protected_path', message "hardrail files are read-only for the agent; ask the user to change <rel>"; ALLOW otherwise; 'warn' mode downgrade as in guard
  ```
  `Decision`/`ALLOW`/`_result` move from `guard.py` into `core/decision.py` (both modules import from there; `guard.py` re-exports `Decision` for the adapter).
- `core/guard.py`: `_READ_ONLY = ('cat','less','more','head','tail','grep','rg','diff','ls','stat','wc','file','md5sum','shasum','sha256sum','jq','bat')`, `_GIT_READ = ('diff','status','log','show','blame')`; `rule_protected_path(simples, config, cwd)`: for each simple, `words = redirect targets (from _drop_redirects' complement) + (argv[1:] unless the command is read-only)`; any word with `is_protected(word, root, config)` → block with message `f"hardrail files are read-only for the agent; ask the user to change {word}"`. Words starting with `-` are skipped. `cd`-walk-back is applied via `packages._cd_targets` to compute the effective cwd.
- `core/shell.py`: export `redirect_targets(argv) -> list[str]` (the words `_drop_redirects` removes that are file names, not fds) — small refactor of `_drop_redirects` into one classifier used by both.

- [ ] **Step 1: Table rows** under `# plan 2: protected paths`:

```python
("echo x > hardrail.config.json", "block", "protected_path"),
("sed -i 's/block/warn/' hardrail.config.json", "block", "protected_path"),
("rm -rf .claude/hardrail", "block", "protected_path"),
("mv .claude/hardrail .claude/h2", "block", "protected_path"),
("cp x .claude/settings.json", "block", "protected_path"),
("tee .claude/settings.local.json < x", "block", "protected_path"),
("git checkout -- hardrail.config.json", "block", "protected_path"),
("python3 -c 'open(\".claude/settings.json\",\"w\")'", "block", "protected_path"),
("cd .claude && rm settings.json", "block", "protected_path"),
("cat hardrail.config.json", "allow", ""),
("git diff .claude/settings.json", "allow", ""),
("python3 -m json.tool hardrail.config.json", "allow", ""),
("echo x > notes.md", "allow", ""),
("ls .claude/hardrail", "allow", ""),
("bash -c 'echo x > hardrail.config.json'", "block", "protected_path"),
("echo x > ./sub/../hardrail.config.json", "block", "protected_path"),
```

- [ ] **Step 2: Unit tests** `test/core/test_files.py`:

```python
def test_default_protected(self): self.assertTrue(is_protected('.claude/hardrail/core/guard.py', root, DEFAULTS))
def test_outside_root_allowed(self): self.assertFalse(is_protected('/tmp/x/hardrail.config.json', root, DEFAULTS))
def test_decide_write_blocks(self): d = decide_write('hardrail.config.json', root, DEFAULTS); self.assertEqual((d.action, d.rule), ('block', 'protected_path'))
def test_decide_write_warn_mode(self): cfg mode warn → action 'warn'
def test_custom_list_replaces(self): cfg protectedPaths ['secrets/**'] → 'hardrail.config.json' not protected, 'secrets/a' protected
def test_nested_cwd_resolves_to_root(self): decide_write('../../hardrail.config.json', root + '/a/b', DEFAULTS).action == 'block'
```
`test_config.py`: `test_defaults_protected_paths` asserts the five defaults; schema test asserts `guard.protectedPaths` is an array of strings.

- [ ] **Step 3: Verify failure.** — [ ] **Step 4: Implement.** — [ ] **Step 5: Suite + bench** (`python3 -m unittest discover -s test -t .` → OK; `python3 test/bench_guard.py` → exit 0).
- [ ] **Step 6: Commit** — `feat(core): protected_path rule and decide_write for hardrail's own files`

---

### Task 2: Claude write hook

**Files:**
- Create: `adapters/claude/pre-write.sh`, `adapters/claude/pre_write.py`
- Modify: `adapters/claude/hooks.snippet.json`, `test/bench_guard.py` (one write-hook case, limit 50 ms)
- Test: `test/adapters/test_claude.py`

**Interfaces:**
- `pre-write.sh`: same skeleton as `pre-bash.sh` (symlink resolve, shim presence check, python3 check, no fast path), runs `pre_write.py`, maps exit 2 through, everything else to 0.
- `pre_write.py` (≤ 30 lines): reads stdin JSON; `path = tool_input.get('file_path') or tool_input.get('notebook_path')`; empty → exit 0; `cwd = event.get('cwd') or os.getcwd()`; `root = find_root(cwd)`; config via `load(root)` if root else `DEFAULTS`; `d = decide_write(path, cwd, config, hint=os.environ.get('CLAUDE_PROJECT_DIR'))`; block → stderr `BLOCKED [protected_path]: …`, exit 2; warn → stderr `WARN …`, exit 0.
- `hooks.snippet.json`: second `PreToolUse` entry `{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hardrail/pre-write.sh", "timeout": 10}]}`.

- [ ] **Step 1: Tests** (`run_write_hook(file_path, cwd)` helper beside `run_hook`): block on `hardrail.config.json` (exit 2, stderr starts `BLOCKED [protected_path]`), block on `.claude/hardrail/pre_bash.py` from `packages/api` cwd, allow on `src/a.ts` (exit 0, empty stderr), allow on `/tmp/elsewhere.txt`, `NotebookEdit` with `notebook_path`, event without a path → exit 0 silent, symlinked hook with missing shim → exit 0 + `guard disabled`, snippet has two entries with the stated matchers and `timeout` 10, `wc -l pre_write.py` ≤ 30.
- [ ] **Step 2: Verify failure.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** `python3 -m unittest test.adapters.test_claude` → OK; bench exit 0 with the new case `('write hook (src file)', …)` under 50 ms p95.
- [ ] **Step 5: Commit** — `feat(adapter): write hook guarding hardrail's own files`

---

### Task 3: Bash oracle

**Files:**
- Create: `test/oracle/__init__.py`, `test/oracle/bin/<name>` for `git pnpm npm yarn bun npx bunx vitest jest turbo nx docker docker-compose podman gh prisma playwright rm mv cp sed tee curl wget sudo doas tail` (each: `#!/usr/bin/env bash` + `printf '%s\n' "$(basename "$0") $*" >> "$HARDRAIL_ORACLE_LOG"; exit 0`; `sudo`/`doas` instead `exec "$@"` after logging), `test/oracle/matcher.py`, `test/oracle/test_bash_oracle.py`, `test/oracle/known_false_blocks.txt`
- Modify: `.github/workflows/ci.yml` (the oracle runs inside the existing discover command, so no change unless its runtime exceeds 60 s; then add a separate step)

**Interfaces:**
- `matcher.dangerous(log_lines: list[str], protected_globs) -> list[str]` — returns the reasons found per Ruling 6, empty when benign. Pure function over the log.
- `test_bash_oracle.py`: for each row in `guard_table.ROWS` not skipped per Ruling 7: tempdir with a copy of the fixture (or empty dir), `HARDRAIL_ORACLE_LOG` file, `PATH = oracle/bin:/usr/bin:/bin`, `subprocess.run(['bash', '-c', row], timeout=5)`; `guard = decide(row, cwd, DEFAULTS)`; `danger = matcher.dangerous(log)`; bypass = `guard.action != 'block' and danger`; false_block = `guard.action == 'block' and not danger and row not in KNOWN`. Assert no bypass (message: row text + log + reasons); assert false blocks ⊆ KNOWN and every KNOWN entry still occurs (stale entries fail too). One `subTest` per row. Also a sanity test: `shutil.which('git', path=oracle_bin_path)` is the fake for every name in `bin/`.

- [ ] **Step 1: Write `matcher.py` tests** (`test_matcher.py` inside `test/oracle/`): `['git push --force']` → one reason; `['git push origin main']` → none; `['pnpm test']` → none (unscoped is out of oracle scope, Ruling 6); `['git commit -m x', 'git push']` → chained; `['tee .claude/settings.json']` → protected; `['cat .claude/settings.json']` → none.
- [ ] **Step 2: Write the oracle test; run it; triage.** Every row the current guard blocks but the oracle finds benign goes into `known_false_blocks.txt` **only** after you confirm by hand that bash really would not run the dangerous thing (e.g. `pnpm test "oops` is skipped as unparsable; `gh pr view` chained is a known over-block). Every bypass the oracle finds is a Critical finding: do **not** fix it in this task; list each in the report and add the row to the ledger for the final review (the controller decides whether a Plan 1.5-style fix round follows).
- [ ] **Step 3: Run** `python3 -m unittest test.oracle 2>&1 | tail -3` → OK, under 60 s; full suite OK.
- [ ] **Step 4: Commit** — `test(oracle): run guard-table rows through real bash with logging fake binaries`

---

### Task 4: `bin/hardrail init | --check` and pre-filter rendering

**Files:**
- Create: `bin/hardrail` (python3, executable, `#!/usr/bin/env python3`), `core/render.py`, `core/__init__.py` (`__version__ = "0.2.0"`)
- Modify: `adapters/claude/pre-bash.sh` (marker block), `test/adapters/test_claude.py` (render equality test replaces the keyword-coverage test)
- Test: `test/test_init.py`, `test/core/test_render.py`

**Interfaces:**
- `core/render.py`: `keywords(config) -> list[str]` (sorted unique first words per Ruling 9 plus the fixed set); `render_prefilter(text, config) -> str` replaces the lines between `# hardrail:keywords:start` and `# hardrail:keywords:end` with `  *WORD*|*WORD*|…) ;;` lines (one `case` pattern line); raises `ValueError` if markers are missing.
- `bin/hardrail`: argparse with subcommand `init` and flags `--check`, `--adapter claude` (default and only), `--dir <repo>` (default cwd). Plain `init`: detect `.claude/` (print the detection line), copy `core/` and the four adapter files into `.claude/hardrail/` **skipping any destination that exists** (print `kept <path>` for each), render the pre-filter from the loaded config, write the manifest, run the smoke (`pnpm test` through `pre-bash.sh` must exit 2; a write to `hardrail.config.json` through `pre-write.sh` must exit 2), print the hooks snippet with a reminder to merge it into `.claude/settings.json`. `--check`: manifest present, every listed file's hash matches (`edited <path>`), no file in `.claude/hardrail/` missing (`missing <path>`), both hooks registered in `.claude/settings.json` (`hook missing: <matcher>`), python3 present, rendered keywords equal `keywords(config)` (`stale render: pre-bash.sh`), config validates (`_load_error`), `claude --version` printed when available; exit 1 on any line.
- `.gitignore` unchanged; `init` prints every path it wrote.

- [ ] **Step 1: Tests**
  - `test_render.py`: `keywords(DEFAULTS)` contains `git`, `rm`, `docker`, `prisma`, `pnpm`, `test`, `tail`; custom `gatedActions: ["terraform apply"]` adds `terraform`; `render_prefilter` round-trips the checked-in `pre-bash.sh` unchanged for `DEFAULTS` (this is the sync test); missing markers raise.
  - `test_init.py` (tempdir repos): fresh repo → files present, manifest has every file with a `sha256:` value, snippet printed, exit 0; run twice → second run writes nothing (compare `os.stat().st_mtime_ns` of every file and the manifest); existing `.claude/settings.json` untouched (hash before/after); `--check` on a fresh install exits 0; edit `pre_bash.py` → `--check` exit 1 with `edited .claude/hardrail/pre_bash.py`; remove the Bash hook from settings → `--check` lists `hook missing: Bash`; custom config `gatedActions: ["terraform apply"]` → rendered `pre-bash.sh` contains `*terraform*` and the hook blocks `ls && terraform apply` with exit 2.
- [ ] **Step 2: Verify failure.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** `python3 -m unittest test.test_init test.core.test_render test.adapters.test_claude` → OK; full suite OK; bench exit 0; `python3 -m unittest test.test_no_leak` OK.
- [ ] **Step 5: Commit** — `feat(init): install and check the copied layout with a manifest; render the pre-filter from config`

---

### Task 5: Docs, ADRs, changelog, schema, contributing, acceptance

**Files:**
- Modify: `README.md`, `docs/reference.md`, `docs/overview.md`, the design spec, `hardrail.config.json` example in README (add `"$schema"`), `test/core/test_config.py`
- Create: `docs/adr/0001-hook-enforced-not-prompted.md`, `0002-python-stdlib-not-typescript.md`, `0003-core-and-thin-adapters.md`, `0004-fail-closed-unparsable.md`, `0005-own-tokenizer-not-shlex.md`, `0006-rule-order.md`, `0007-keyword-prefilter.md`, `CHANGELOG.md`, `hardrail.schema.json`, `CONTRIBUTING.md`, `test/acceptance/run.sh` (not run in CI; documented)

**Interfaces:**
- ADR source material: the design spec "Owner decisions", the Rulings sections of `docs/superpowers/plans/2026-10-29-hardening.md` and this plan, and the ledger excerpt the controller pastes into the brief. Each ADR ≤ 25 lines (Ruling 12). No new decisions are invented in an ADR; an ADR that has no source line is dropped.
- `CHANGELOG.md`: Keep-a-Changelog layout, sections `Unreleased`, `0.2.0`, `0.1.0`, entries `Added / Changed / Fixed`, ≤ 10 lines per release; small rulings from the ledgers land here.
- `hardrail.schema.json`: JSON Schema draft-07 for `hardrail.config.json` describing exactly the keys `core/config.py` `DEFAULTS` and `validate` accept (`guard.mode`, `guard.gatedActions`, `guard.protectedPaths`, `heavy`, `defaults.test`, `packages.<name>.test`), `additionalProperties: false` at every level; test: `json.load` the schema, every key in `DEFAULTS` is described, and the README example validates by walking the schema with a 20-line stdlib checker in the test (no `jsonschema` dependency).
- `CONTRIBUTING.md` ≤ 40 lines: run suite / bench / leak test; add a guard-table row for every behaviour change; bug report for a wrong block needs exactly three things (the command, the directory layout, the expected decision); the ADR rule from Ruling 12; `HARDRAIL_FUZZ_SEED=<n>` reruns the robustness test with that seed (the test from Plan 1.5 Task 7 reads the env var; if it does not yet, add the two lines here).

- [ ] **Step 1: Docs**
  - README: Quick start becomes `python3 bin/hardrail init` (manual copy moves to reference); new section **Threat model** — what it stops (every Bash and file-write tool call through the hooks), what it does not (an agent running outside the hooks: MCP tools, `ssh host cmd`, `docker exec`, code the agent commits that runs later in CI; a human disabling the hook; a Claude Code version that changes the hook contract — `init --check` catches drift, not intent); recommendation to put the hook entries in `~/.claude/settings.json` (user level) so a repo-level edit cannot remove them, with the exact snippet; two more lines: hardrail is a guardrail, not a sandbox (real containment is the agent's sandbox or managed settings), and hardrail has no environment-variable off switch by design; "What it blocks" table gains `protected_path`; Configuration documents `guard.protectedPaths`.
  - reference: `init` commands and exit codes, manifest format, render markers, oracle description and how to read `known_false_blocks.txt`.
  - overview: legend update (`bin/hardrail`, manifest, write hook now shipped).
  - Spec: Owner decisions get Rulings 1–13 (dated); roadmap row `0.2 — init, write hook, oracle, second adapter (this plan)`; `init --upgrade` moves to the roadmap.
  - ADRs, CHANGELOG, schema, CONTRIBUTING per Interfaces. README links `docs/adr/` and CONTRIBUTING in one line each.
- [ ] **Step 2: Acceptance script** `test/acceptance/run.sh`: fresh tempdir repo with a `package.json` (`"test": "vitest"`), `python3 <hardrail>/bin/hardrail init --dir <tmp>`, merge the printed snippet into `.claude/settings.json` with python (`json` module), then `hardrail init --check --dir <tmp>` → 0; then, when `claude` is on `PATH`, `claude -p` with three prompts (run the tests; commit and push in one command; set guard.mode to warn in hardrail.config.json) and grep the transcript for `BLOCKED [unscoped_test]`, `BLOCKED [chained_gated]`, `BLOCKED [protected_path]`. Print a pass/fail table; exit 1 on any miss. Document in README Development.
- [ ] **Step 3: Verify** — full suite OK (includes the schema test); `python3 test/bench_guard.py` exit 0; `python3 test/test_no_leak.py` OK; `wc -l docs/adr/*.md CONTRIBUTING.md` within caps; `bash test/acceptance/run.sh` (local, with `claude` available) → all three BLOCKED lines seen; paste the table into the task report.
- [ ] **Step 4: Commit** — `docs: threat model, ADRs, changelog, config schema, contributing guide, acceptance script`

---

### Task 6: Second adapter

**Files:**
- Create: `adapters/<agent>/` — one shim per hook the agent offers for shell commands (and file writes when it has one), each ≤ 30 lines, plus `hooks.snippet.<ext>` and a 15-line `README.md` inside the folder
- Modify: `README.md` (supported agents table: one row), `docs/overview.md` (legend), `bin/hardrail` (`--adapter <agent>` accepted; same layout under `.claude/hardrail/` or the agent's own config dir as the agent documents)
- Test: `test/adapters/test_<agent>.py`

**Interfaces:**
- Target agent: **Codex CLI** (hook protocol closest to Claude Code's). Per its hooks documentation (https://learn.chatgpt.com/docs/hooks): config `<repo>/.codex/hooks.json` with `[[hooks.PreToolUse]]` `matcher="^Bash$"` and `type="command"` entries; stdin JSON carries `tool_name` (`Bash`), `tool_input.command`, `cwd`; block = exit 2 with the reason on stderr (JSON `permissionDecision: "deny"` also accepted). A hook error, timeout or malformed output does **not** block in Codex, so the shim must always emit its own deny. The implementer re-reads that page before coding and records any field that differs from this line in the report; the ledger carries the diff. The shim translates only: read JSON, pick command + cwd, call `core.guard.decide`, emit the deny signal; no policy.
- Test: round-trip the three acceptance commands (`pnpm test`, `git commit -m x && git push`, `echo x > hardrail.config.json`) through the shim and assert the agent's deny signal and the rule id; `ls` passes; malformed stdin → the shim's fail-open line; `wc -l` ≤ 30 per shim.

- [ ] **Step 1: Tests.** — [ ] **Step 2: Verify failure.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** `python3 -m unittest test.adapters` → OK; full suite OK; leak test OK.
- [ ] **Step 5: Commit** — `feat(adapter): <agent> shim over the same core`

---

## Controller steps outside the repo (not implementer tasks)

- After Task 5 lands: add one line to the workspace `done-checklist` skill — a plan with a Rulings section ships its ADR(s) in the same PR. One line, no new file.
- Deferred research ideas, not scheduled: evaluation deadline with block-on-timeout; Semgrep-style `ok:`/`ruleid:` corpus annotations; perf baseline files; allowlist mode and rule packs. Each would add a mechanism the current tests already approximate.
