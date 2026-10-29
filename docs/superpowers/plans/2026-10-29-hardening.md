# Guard Hardening (Plan 1.5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every bypass, false block and crash found in the full review of `main` (c315abc), so that a gated or unscoped command reaches `decide()` as what it is, every exotic construct fails closed, and the adapter fails open only on its own errors.

**Architecture:** Targeted fixes inside the existing modules, not a rewrite. `core/shell.py` gains one heredoc-delimiter parser shared by both scanners, scans unquoted heredoc bodies for substitutions, unescapes backtick bodies, strips shell reserved words, drops redirections before reader checks, and turns the remaining uninspectable constructs (`$'…'`, `$var` command words, `… | bash`, `bash <(…)`, > 64 KB input) into `ParseError`. `core/argv.py` becomes the single table of wrapper and manager options. `core/guard.py` wraps the rule loop so nothing escapes, normalises gated prefixes, treats a gated action inside a payload as chained, and builds every fix message through one helper. `core/packages.py` finds the workspace root without a config file and reads the common `pnpm-workspace.yaml` forms. The adapter resolves symlinks, checks the shim exists, ignores events without a command, and skips the fast path when a config file is present.

**Tech Stack:** python3 ≥ 3.9 stdlib only (`shlex`, `json`, `re`, `unittest`), bash, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-29-hardrail-design.md` — Parts 1–3, Constraints, Verification rows bash-guard and latency; section "Architecture hardening (for 0.3)". The unified quote scanner named there stays deferred to 0.3; this plan fixes the drift between the existing scanners where the review found it.

## Global Constraints

- python3 stdlib only; no `dataclasses`, `typing`, `fnmatch` in `core/` (import cost).
- `adapters/claude/pre_bash.py` ≤ 30 lines; no policy in the adapter.
- Fail closed on anything the tokenizer cannot inspect: `ParseError` → `unparsable` block (warn in `guard.mode: warn`). Fail open only on the adapter's own errors, with one stderr line.
- Hook p95 < 100 ms on CI; bench exit-code asserts stay.
- No repository, company, product, ticket or person name anywhere, including fixtures and commit messages (`python3 -m unittest test.test_no_leak` must pass).
- Every bypass in the review becomes a row in `test/core/guard_table.py` **before** its fix; the row asserts action **and** rule.
- Run after every task: `python3 -m unittest discover -s test -t .` → `OK`; `python3 test/bench_guard.py` → exit 0.
- Commit after every task; messages in English, ending with the session's attribution trailer.

## Rulings (recorded here so implementers do not re-decide them)

1. `$'…'` / `$"…"` quoting → `ParseError` ("$'...' quoting is not supported; write the string plainly"). Rare in agent output; fail closed beats decoding C escapes.
2. A command word that is only a variable (`$cmd`, `${x}`, `"$@"`) → `ParseError` ("command word is a variable; write the command literally"). `$HOME/bin/x` (variable plus path) stays allowed.
3. `<prev> | bash`: if `prev` is `echo`/`printf` with literal arguments, those are the payload (kind `pipe`); if `prev` carried a heredoc (its body was already inspected), allow; otherwise, including `echo $(…) | bash`, `ParseError` ("command piped into a shell cannot be inspected; run it as a script file"). `curl … | sh` therefore blocks as unparsable.
4. A shell whose script or stdin comes from a process substitution (`bash <(…)`, `bash < <(…)`) → `ParseError`.
5. A gated action inside any payload (`sh -c`, `eval`, heredoc, subst, pipe) counts as chained whenever the whole command has more than one simple. `bash -c "git commit -m x"` alone therefore blocks; the fix message says to run the inner command directly.
6. `git push --delete`, `git push -d`, `git push origin :branch` block under `force_push` with message "deleting a remote branch is blocked; ask before deleting remote refs". Destructive, same family.
7. Lists in config keep replace semantics; the README says so. No merge key (YAGNI).
8. `packages.<name>` in the schema becomes `{ "test": string }`; `root` and `testFor` are removed (code never read them). Spec gets a one-line note.
9. Path heuristic: an argument that is an existing file or directory relative to cwd counts as a path, in addition to `/` and `.test.`/`.spec.` names. `vitest --run src` is then allowed in a repo that has `src/`.
10. The pre-filter stays keyword-based until 0.2 renders it from config; interim: skip the fast path when `$CLAUDE_PROJECT_DIR/hardrail.config.json` exists.
11. Bench stays warm-only; the spec Verification row says "warm".

## Review Focus

1. `cat <<EOF⏎$(git push -f)⏎EOF` — bash expands the substitution; must block `force_push`. Task 1.
2. `env -i git push -f`, `sudo -u x pnpm test`, `timeout 5 git push -f` — wrapper options must not become the command word. Task 3.
3. `ls && bash -c "rm -rf x"` — gated action hidden in a payload must block `chained_gated`. Task 3.
4. A config with `defaults.test` = `pnpm exec 'vitest` — nothing may raise out of `decide()`; expect `unparsable`. Task 3.
5. Hook run through a symlink or with `pre_bash.py` missing — must exit 0 with one stderr line, never 2. Task 6.

---

### Task 1: Tokenizer quoting and heredoc fixes

**Files:**
- Modify: `core/shell.py` (`_HEREDOC` :11, `_protect` :55-111, `_find_heredoc` :262-289, `_skip_bodies` :307-322, `_walk` :325-395, `_extract_substs` :398-414, `_extract_heredocs` :417-443)
- Test: `test/core/test_shell.py`, `test/core/guard_table.py`

**Interfaces:**
- Produces: `_heredoc_at(text, i) -> (delimiter: str, quoted: bool, end: int) | None` — parses the word after `<<`/`<<-` at index `i`: a sequence of `'…'`, `"…"`, `\x` and plain chars up to whitespace or one of `;&|<>()`; `quoted` is True when any quote or backslash occurred; delimiter has quotes and backslashes removed. Returns None for an empty word; raises `ParseError` when the word contains `$` or a backtick. Used by `_find_heredoc` and `_walk`.
- `_walk(cmd, i, close, level=0, dq=False)` — `dq=True` scans a double-quote-like region: `'` and `#` are literal, newlines do not start heredoc bodies.

- [ ] **Step 1: Add table rows (expected to fail now)**

Append to `ROWS` in `test/core/guard_table.py` under a new comment `# review: quoting and heredocs`:

```python
("cat <<EOF\n$(git push -f)\nEOF", "block", "force_push"),
("cat <<EOF\n`git push -f`\nEOF", "block", "force_push"),
("x=$(cat <<EOF\n$(git push -f)\nEOF\n)", "block", "force_push"),
("cat <<'EOF'\n$(git push -f)\nEOF", "allow", ""),
('cat <<E"OF"\nhi\nEOF\ngit push -f', "block", "force_push"),
("cat <<'E'OF\nhi\nEOF\ngit push -f", "block", "force_push"),
("cat <<\\EOF\ngit push -f\nEOF", "allow", ""),
("bash <<'E-OF'\ngit push -f\nE-OF", "block", "force_push"),
("git push $'-f'", "block", "unparsable"),
("bash -c $'git push -f'", "block", "unparsable"),
("echo `echo \\`git push -f\\``", "block", "force_push"),
```

- [ ] **Step 2: Add unit tests in `test/core/test_shell.py`**

```python
def test_heredoc_at_plain(self): self.assertEqual(_heredoc_at('<<EOF x', 0), ('EOF', False, 5))
def test_heredoc_at_partly_quoted(self): self.assertEqual(_heredoc_at('<<E"OF"\n', 0), ('EOF', True, 7))
def test_heredoc_at_backslash(self): self.assertEqual(_heredoc_at('<<\\EOF\n', 0), ('EOF', True, 6))
def test_heredoc_at_dash_delim(self): self.assertEqual(_heredoc_at("<<'E-OF'\n", 0)[0], 'E-OF')
def test_heredoc_at_dollar_raises(self): with self.assertRaises(ParseError): _heredoc_at('<<$X\n', 0)
def test_unquoted_body_subst_is_split(self):
    s = split_commands("cat <<EOF\n$(git push -f)\nEOF")
    self.assertIn(['git', 'push', '-f'], [x.argv for x in s])
def test_dollar_quote_raises(self): with self.assertRaises(ParseError): split_commands("git push $'-f'")
def test_nested_escaped_backtick(self):
    self.assertIn(['git', 'push', '-f'], [x.argv for x in split_commands("echo `echo \\`git push -f\\``")])
```

- [ ] **Step 3: Run to verify they fail**

Run: `python3 -m unittest test.core.test_shell test.core.test_guard 2>&1 | tail -3` — Expected: FAIL (new rows and tests).

- [ ] **Step 4: Implement**

- `_heredoc_at` as specified; replace both `_HEREDOC.match` call sites. In `_find_heredoc` return `(start, delim, quoted, end)` instead of a regex match; in `_extract_heredocs` the kept line becomes `line[:start] + ' _heredoc_ ' + line[end:]` (placeholder word `_HEREDOC_PH = '_heredoc_'`, used by Task 2's pipe rule).
- `_walk`: `pending` holds `(delim, quoted)`; `_skip_bodies` returns the body spans `[(b0, b1, quoted)]`; for each unquoted body call `_walk(cmd, b0, None, level + 1, dq=True)` bounded to `b1` (add an `end` parameter or slice-and-offset) and extend `found` with its substitutions. With `dq=True`: `'` is literal, `#` is literal, `\n` never consults `pending`, `"` does not toggle `q`.
- `_protect`: outside quotes, `$'` or `$"` → `raise ParseError("$'...' quoting is not supported; write the string plainly")`.
- Backtick bodies: in `_extract_substs`, for a span that started with `` ` ``, the body text has `` \` `` → `` ` `` and `\\` → `\` applied before it is appended to `bodies`. Mark spans with their opener in `found` (5-tuple).

- [ ] **Step 5: Run the suite and bench**

Run: `python3 -m unittest discover -s test -t . 2>&1 | tail -3` — Expected: `OK`. Run: `python3 test/bench_guard.py` — Expected: exit 0.

- [ ] **Step 6: Commit** — `fix(shell): parse heredoc delimiters whole, scan unquoted bodies, reject $'…'`

---

### Task 2: Tokenizer command-word and shell-reader fixes

**Files:**
- Modify: `core/shell.py` (`_unwrap` :155-171, `_shell_scan` :214-230, `_payload_of` :233-248, `_is_shell_reader` :251-259, `_split_plain` :174-211, `split_commands` :446-467, `Simple` :15-30)
- Test: `test/core/test_shell.py`, `test/core/guard_table.py`

**Interfaces:**
- `Simple` gains slot `sep` (str): the separator token before this simple at its level — `''` for the first, else one of `;`, `&`, `&&`, `||`, `|`, `\n` (a run of separators is reduced to its first two chars). `__eq__`/`__repr__` unchanged (sep is not compared).
- `_drop_redirects(argv) -> list[str]` — removes words matching `^\d*[<>]{1,2}&?\S*$` plus `&>`/`&>>` words, and the word after a bare `<`, `>`, `>>`, `2>`, `&>`.
- `_SHELLS` becomes `('bash', 'sh', 'zsh', 'dash', 'ksh', 'mksh', 'ash', 'busybox')`.
- `_KEYWORDS = ('if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{', 'time', 'coproc')`; trailing `('fi', 'done', 'esac', '}')`.
- `_VAR_WORD = re.compile(r'^"?\$\{?[\w@*]+\}?"?$')`.
- `_MAX_INPUT = 65536` bytes.

- [ ] **Step 1: Add table rows** under `# review: command word and shell readers`:

```python
("if true; then git push -f; fi", "block", "force_push"),
('bash -c "if true; then git push -f; fi"', "block", "force_push"),
("{ git push -f; }", "block", "force_push"),
("true && { git push -f; }", "block", "force_push"),
("! git push -f", "block", "force_push"),
("time (git push -f)", "block", "force_push"),
("time ( git push -f )", "block", "force_push"),
("while :; do git push -f; done", "block", "force_push"),
("for f in *; do pnpm test; done", "block", "unscoped_test"),
("f(){ git push -f; }", "block", "force_push"),
("bash -c -- 'git push -f'", "block", "force_push"),
("bash +x -c 'git push -f'", "block", "force_push"),
("bash --rcfile r -c 'git push -f'", "block", "force_push"),
("ksh -c 'git push -f'", "block", "force_push"),
("echo 'git push -f' | bash", "block", "force_push"),
("printf 'pnpm test' | sh", "block", "unscoped_test"),
("curl -s x | sh", "block", "unparsable"),
("echo $(ls) | bash", "block", "unparsable"),
("echo \"$(cat <<'EOF'\nls\nEOF\n)\" | grep x", "allow", ""),
('bash <<<"git push -f" 2>/dev/null', "block", "force_push"),
("bash <(echo git push -f)", "block", "unparsable"),
("bash < <(echo git push -f)", "block", "unparsable"),
("cmd='git push -f'; eval \"$cmd\"", "block", "unparsable"),
("x=git; $x push -f", "block", "unparsable"),
('"$@"', "block", "unparsable"),
("$HOME/bin/tool --flag", "allow", ""),
("echo " + "a" * 70000, "block", "unparsable"),
("echo " + "a" * 60000, "allow", ""),
```

- [ ] **Step 2: Unit tests in `test/core/test_shell.py`**

```python
def test_sep_recorded(self):
    s = split_commands("ls | grep x && pwd")
    self.assertEqual([x.sep for x in s], ['', '|', '&&'])
def test_drop_redirects(self):
    self.assertEqual(_drop_redirects(['bash', '2>/dev/null', '>', 'out', '<<<', 'x']), ['bash', '<<<', 'x'])
def test_keywords_stripped(self):
    self.assertEqual(split_commands("if true; then git push -f; fi")[1].argv, ['git', 'push', '-f'])
def test_shell_scan_double_dash(self):
    self.assertEqual(_payload_of(['bash', '-c', '--', 'ls']), ('ls', 'sh -c'))
def test_pipe_payload(self):
    self.assertEqual(split_commands("echo 'ls -a' | bash")[-1].argv, ['ls', '-a'])
```

- [ ] **Step 3: Run to verify failure** — `python3 -m unittest test.core.test_shell test.core.test_guard 2>&1 | tail -3` → FAIL.

- [ ] **Step 4: Implement**

- `split_commands`: first line `if len(cmd.encode('utf-8', 'replace')) > _MAX_INPUT: raise ParseError("command too long to inspect; run it as a script file")`.
- `_split_plain`: when flushing, set `sep` from the separator token that preceded the group (`tok[:2]`).
- `_unwrap`: loop — strip leading tokens that are in `_KEYWORDS`, end with `{`, or match `^\w+\(\)\{?$`; existing paren handling runs inside the same loop; strip trailing tokens in the trailing set; then existing `case` logic. `time -p` → also drop a `-p` directly after `time`.
- `_shell_scan`: accept `+x` as a flag; `--` means "options end", so with a pending `-c` the payload is the next word; long options `--rcfile`, `--init-file`, `-o`, `-O`, `--rcfile=…` skip their value; `-c` returns the first non-option word after it (`bash -c -- 'x'`, `bash -x -c 'x'`).
- `_is_shell_reader(argv)` and `_payload_of` run on `_drop_redirects(argv)`; before dropping, if any word equals `_SUBST_PH` and the argv's command is a shell → `raise ParseError("script from a process substitution cannot be inspected; run it as a script file")`.
- `_payload_of`: if `_VAR_WORD.match(argv[0])` → `raise ParseError("command word is a variable; write the command literally")`. This also catches `eval "$cmd"` after the eval payload is split.
- Pipe rule in `split_commands` after `_split_plain`: for simple `s` with `s.sep == '|'` and `_is_shell_reader(s.argv)`: `prev = out[-1]` (the previous top-level simple at this level); if `prev.argv[0] in ('echo', 'printf')` and no word of `prev.argv` is `_SUBST_PH` → payload `' '.join(prev.argv[1:])`, kind `pipe`, appended like other payloads; elif `_HEREDOC_PH in prev.argv` (body already inspected) → nothing; else `raise ParseError("command piped into a shell cannot be inspected; run it as a script file")`. Remove `_pipes_to_shell`'s heredoc-reader special case only if the table still passes without it (keep it otherwise).

- [ ] **Step 5: Run suite + bench** — `OK`, exit 0. Also: `python3 -c "import time,core.guard as g,core.config as c;t=time.perf_counter();g.decide('echo '+'a'*60000,'/tmp',c.DEFAULTS);print(time.perf_counter()-t)"` → under 1.0 s.

- [ ] **Step 6: Commit** — `fix(shell): strip reserved words, scan shell flags fully, fail closed on pipes, process substitution and variable command words`

---

### Task 3: Wrappers, gated normalisation, nothing escapes `decide`

**Files:**
- Modify: `core/argv.py`, `core/guard.py` (`_is_gated` :174-180, `rule_chained_gated` :183-195, `decide` :315-326, `_split_cached` :86-89), `core/packages.py` (`_read_json` :42-48, `_workspace_globs` :80-101), `core/config.py` (`DEFAULTS` gatedActions)
- Test: `test/core/test_guard.py`, `test/core/test_config.py`, `test/core/guard_table.py`

**Interfaces:**
- `core/argv.py`:
  ```python
  WRAPPERS = {  # name -> options that take a value (next word)
      'sudo': ('-u','-g','-h','-p','-C','-D','-r','-t','-T','-U','--user','--group'),
      'doas': ('-u','-C'), 'env': ('-u','--unset','-C','--chdir'),
      'nohup': (), 'command': (), 'exec': ('-a',), 'time': (), 'corepack': (),
      'timeout': ('-s','--signal','-k','--kill-after'), 'nice': ('-n','--adjustment'),
      'ionice': ('-c','-n','-p'), 'stdbuf': ('-i','-o','-e'), 'chronic': (),
      'xargs': ('-I','-n','-P','-d','-L','-s','-E','-a','--max-args','--max-procs','--delimiter'),
  }
  POSITIONAL_AFTER = {'timeout': 1}   # words consumed after the options (the duration)
  def strip_env_and_wrappers(argv) -> (prefix, rest)   # raises ParseError on `env -S …` or when rest starts with '-' or is empty after a wrapper
  VALUE_FLAGS = ('-C','-F','--filter','--filter-prod','--dir','--prefix','--cwd','--reporter','--workspace-concurrency','--loglevel','--config','-c','--registry','--store-dir')
  NPM_WORKSPACE_FLAGS = ('-w','--workspace')     # value flags for npm only
  def skip_manager_opts(args, manager=None) -> list   # manager='npm' adds NPM_WORKSPACE_FLAGS
  def manager_flags(argv) -> list[(flag, value|None)]  # moved from packages._parse_flags, same contract, uses VALUE_FLAGS and NPM_WORKSPACE_FLAGS
  ```
  `ParseError` is imported from `core.shell`.
- `core/guard.py`: `_gated_forms(c) -> list[list[str]]` = `[c, [c[0]] + form, form]` where `form = _heavy_form(c).split()`; `_is_gated` matches any prefix against any form. `decide` wraps the rule loop: `except Exception as e: return _unparsable(config, "guard could not evaluate the command (%s)" % type(e).__name__)`.
- `DEFAULTS["guard"]["gatedActions"]` adds `"pnpm i", "npm i", "npm ci", "yarn install", "bun install", "bun add"`.

- [ ] **Step 1: Table rows** under `# review: wrappers and gated normalisation`:

```python
("env -i git push -f", "block", "force_push"),
("env -u X git push -f", "block", "force_push"),
("env -- git push -f", "block", "force_push"),
("sudo -u bob git push -f", "block", "force_push"),
("sudo -E pnpm test", "block", "unscoped_test"),
("nohup -- git push -f", "block", "force_push"),
("time -p git push -f", "block", "force_push"),
("timeout 5 git push -f", "block", "force_push"),
("timeout -s KILL 5s git push -f", "block", "force_push"),
("nice -n 5 git push -f", "block", "force_push"),
("xargs git push -f", "block", "force_push"),
("echo x | xargs -I{} git push -f", "block", "force_push"),
("env -S 'git push -f'", "block", "unparsable"),
("ls && sudo -u root rm -rf x", "block", "chained_gated"),
('ls && bash -c "rm -rf x"', "block", "chained_gated"),
("ls && echo $(rm -rf x)", "block", "chained_gated"),
('bash -c "git commit -m x"', "block", "chained_gated"),
("ls && pnpm prisma migrate reset", "block", "chained_gated"),
("ls && npx prisma migrate reset", "block", "chained_gated"),
("ls && pnpm --filter x add zod", "block", "chained_gated"),
("ls && pnpm -w add zod", "block", "chained_gated"),
("ls && pnpm i", "block", "chained_gated"),
("ls && npm ci", "block", "chained_gated"),
("ls && bun add zod", "block", "chained_gated"),
```

- [ ] **Step 2: Unit tests**

`test/core/test_guard.py`:
```python
def test_bad_config_test_command_is_unparsable(self):
    cfg = copy.deepcopy(DEFAULTS); cfg['defaults']['test'] = "pnpm exec 'vitest"
    d = decide('ls', '/tmp', cfg); self.assertEqual((d.action, d.rule), ('block', 'unparsable'))
def test_non_utf8_workspace_yaml(self):   # tempdir with pnpm-workspace.yaml bytes b'\xff\xfe', then
    d = decide('pnpm --filter a test src/a.test.ts', tmp, DEFAULTS); self.assertEqual(d.action, 'block')
def test_deep_package_json(self):         # tempdir with package.json = '[' * 100000
    d = decide('pnpm test src/a.test.ts', tmp, DEFAULTS); self.assertIn(d.action, ('allow', 'block'))
def test_chained_message_names_inner_command(self):
    self.assertIn('git commit -m x', decide('bash -c "git commit -m x"', '/tmp', DEFAULTS).message)
```
`test/core/test_config.py`: `test_defaults_gate_install_aliases` asserts `"npm ci"` and `"pnpm i"` are in `DEFAULTS["guard"]["gatedActions"]`.

- [ ] **Step 3: Verify failure** — scoped run → FAIL.

- [ ] **Step 4: Implement**

- `argv.py` as in Interfaces. `strip_env_and_wrappers` loop: env assignment → skip; wrapper word → skip it, then its options (`-x`, `-x val` for value options, `--opt=val`, `--opt val` for value options, `--` ends options), then `POSITIONAL_AFTER` words; `env -S`/`--split-string` → `ParseError("env -S payload cannot be inspected")`. After the loop, if a wrapper was consumed and `rest` is empty or `rest[0].startswith('-')` → `ParseError("wrapper options hide the command word")`.
- `packages._parse_flags` → `argv.manager_flags`; `packages.py` imports it. `skip_manager_opts` gains `manager`; `_runner` passes `base`.
- `guard._is_gated` via `_gated_forms`. `rule_chained_gated`: keep the per-chain check; add: `if len(simples) > 1: gated = next((s for s in simples if s.payload_of and _is_gated(s, config)), None)` → block with message `f"gated action inside a shell payload; run `{gated.raw}` directly as its own call"`.
- `decide`: wrap rule loop as in Interfaces. `packages._read_json` catches `(OSError, ValueError, RecursionError)`; `_workspace_globs` catches `(OSError, UnicodeDecodeError)`.
- `config.DEFAULTS` gatedActions additions. The pre-filter already matches `npm`, `yarn`, `bun`; `test/adapters/test_claude.py` sync test must still pass.

- [ ] **Step 5: Suite + bench** → `OK`, exit 0.
- [ ] **Step 6: Commit** — `fix(guard): strip wrapper options, gate actions inside payloads and behind managers, never raise out of decide`

---

### Task 4: Runner flags, path detection, fix messages

**Files:**
- Modify: `core/guard.py` (`_PATH_ARG` :12, `_runner` :106-138, `_has_path`/`_first_path`/`_own_path_token` :141-154/253, `_watch_fix` :210-242, `_unscoped_decision` :257-279)
- Test: `test/core/test_guard.py`, `test/core/guard_table.py`

**Interfaces:**
- `_RUNNER_VALUE_FLAGS = ('--config','-c','--root','-r','--dir','--project','--reporter','--outputFile','--coverage.reporter','-t','--testNamePattern','--rootDir','--maxWorkers','-w','--selectProjects','--shard','--pool')`
- `_JEST_PATH_FLAGS = ('--testPathPattern', '--testPathPatterns')` — their value (next word or `=value`) is a path.
- `_VITEST_SUBCMDS = ('related', 'bench', 'list')`
- `_paths(args, cwd) -> list[str]`: words that are not flags, not values of `_RUNNER_VALUE_FLAGS`, and either contain `/`, match `_PATH_ARG`, are the value of a `_JEST_PATH_FLAGS` flag, or exist as a file/dir relative to `cwd` (`os.path.exists(os.path.join(cwd, word))`; words starting with `-` never checked). Replaces `_has_path`, `_first_path`, `_own_path_token` (the last keeps its own one-liner over the script string, using `_paths(script.split(), pkg.root)`).
- `_fix_cmd(argv, name, args, path, config) -> str`: for `name in _RUNNERS` the original invocation up to and including the runner token, then the vitest subcommand if `args[0] in _VITEST_SUBCMDS`, then `--run` (vitest) or `--watchAll=false` (jest, only when a watch flag was present), then `path`; for scripts and `configured`, `config['defaults']['test'] + ' ' + path`. All five fix sites in `_watch_fix` and `_unscoped_decision` call it; the hard-coded `pnpm exec vitest --run` and the `base.replace('exec', …)` go away. The "all" message becomes `f"this runs every workspace package; target one package: {('pnpm --filter <name> ' + base[5:]) if base.startswith('pnpm ') else base} {path}"`; the "script already has a path" message becomes `f"…; use: {fix}"` with `fix = _fix_cmd(...)` prefixed by `f"pnpm -C {rel} "` only when `rel != '.'` and base starts with `pnpm`, else `f"(in {rel}) "`.
- `_runner`: npm aliases `t`, `tst` → `test`; `run-script` like `run`.

- [ ] **Step 1: Table rows** under `# review: runner flags and paths` (cwd `FX + "/packages/api"` where marked):

```python
("vitest --run --config ./vitest.config.ts", "block", "unscoped_test"),
("jest --config ./jest.config.js", "block", "unscoped_test"),
("pnpm test -- -t src/a", "block", "unscoped_test", FX + "/packages/api"),
("jest --testPathPattern src/a", "allow", ""),
("jest --testPathPattern=src/a", "allow", ""),
("npm t", "block", "unscoped_test"),
("npm run-script test", "block", "unscoped_test"),
("pnpm --reporter silent test", "block", "unscoped_test", FX + "/packages/api"),
("pnpm --workspace-concurrency 2 -r test src/a.test.ts", "block", "unscoped_test", FX),
("npm -w @fx/api test src/a.test.ts", "allow", "", FX),
("npm --workspace @fx/api test", "block", "unscoped_test", FX),
("pnpm exec vitest --run src", "allow", "", FX + "/packages/api"),
("pnpm exec vitest --run auth", "block", "unscoped_test", FX + "/packages/api"),
("vitest related src/a.ts", "block", "watch_mode"),
```
Fixture: add `test/fixtures/pnpm-monorepo/packages/api/src/.keep` (so `src` exists).

- [ ] **Step 2: Unit tests**

```python
def test_related_fix_keeps_subcommand(self):
    self.assertIn('vitest related --run src/a.ts', decide('vitest related src/a.ts', '/tmp', DEFAULTS).message)
def test_jest_fix_is_jest(self):
    m = decide('npx jest', '/tmp', DEFAULTS).message; self.assertIn('npx jest', m); self.assertNotIn('vitest', m)
def test_fix_honours_defaults_test(self):
    cfg = copy.deepcopy(DEFAULTS); cfg['defaults']['test'] = 'npx jest'
    self.assertIn('npx jest <path', decide('npm test', '/tmp', cfg).message)
def test_all_fix_non_pnpm_base(self):
    cfg = copy.deepcopy(DEFAULTS); cfg['defaults']['test'] = 'npx vitest run'
    self.assertNotIn('--filter <name> exec', decide('pnpm -r test', FX, cfg).message)
```

- [ ] **Step 3: Verify failure.** — [ ] **Step 4: Implement** as in Interfaces. — [ ] **Step 5: Suite + bench** → `OK`, exit 0.
- [ ] **Step 6: Commit** — `fix(guard): runner value flags, existing paths, one fix-message builder`

---

### Task 5: Workspace root, yaml forms, filter edge cases, watch and push rules

**Files:**
- Modify: `core/packages.py` (`_workspace_globs` :80-101, `_filter_package` :147-155, `resolve_package` :183-228), `core/guard.py` (`_unscoped_decision` :261, `_watch_fix` :215-219, `rule_force_push` :162-171), `core/config.py` (`heavy` default, `_check_types` unknown keys), `schema/hardrail.config.schema.json`
- Test: `test/core/test_packages.py`, `test/core/test_config.py`, `test/core/guard_table.py`, fixtures under `test/fixtures/`

**Interfaces:**
- `packages.find_workspace_root(cwd) -> str | None`: nearest ancestor with `pnpm-workspace.yaml`, or a `package.json` whose `workspaces` key exists. `guard._unscoped_decision` uses `find_root(cwd) or find_workspace_root(cwd) or cwd`.
- `_workspace_globs` reads: `packages:` block list; inline `packages: [a, "b"]`; multi-line flow list; trailing `# comment` outside quotes stripped; key may be indented or quoted.
- `_filter_package(flt, repo_root, cwd)`: `.` → `_ancestor_package(cwd, repo_root)`; `{dir}` → `_package_at(join(cwd, dir))`; `./x`, `../x` relative to `cwd`; absolute kept; other `/` forms relative to `repo_root` as today.
- `_watch_fix`: `_DOCKER_GLOBAL_VALUE = ('-H','--host','--context','-c','--config','-l','--log-level')`, `_COMPOSE_VALUE = ('-f','--file','-p','--project-name','--env-file','--profile','--project-directory')`. For base in `('docker','docker-compose','podman')`: skip global value options, optional `compose` word, compose value options; if the next word is `logs` and `-f`/`--follow` follows → fix as today (`--tail=200` added when no `--tail`/`-n`). `tail`: `-f`, `-F`, `--follow[=…]` → `"use tail -n 200 <file>"` with the file words kept.
- `rule_force_push`: `--delete`, `-d`, or a refspec starting with `:` (len > 1) → block `force_push`, message per Ruling 6.
- `DEFAULTS["heavy"]` = `["build", "build:*", "turbo build", "vite build", "generate:*", "prisma", "prisma *", "playwright", "playwright *"]`. Pre-filter gains `*turbo*`.
- `config._check_types`: unknown top-level keys → `errors.append("unknown key %s" % key)` (value kept; only the warning).
- Schema: `packages.additionalProperties` = object with `test: string` only.

- [ ] **Step 1: Table rows** under `# review: workspace, watch, push`:

```python
("pnpm --filter @fx/web test src/a.test.ts", "block", "unscoped_test", FX + "/packages/api"),
("pnpm --filter @fx/api test src/a.test.ts", "allow", "", FX + "/packages/web"),
("pnpm --filter . test src/a.test.ts", "allow", "", FX + "/packages/api"),
("pnpm --filter '{packages/api}' test src/a.test.ts", "allow", "", FX),
("pnpm --filter ../api test src/a.test.ts", "allow", "", FX + "/packages/web"),
("docker logs -f api", "block", "watch_mode"),
("docker compose -f dc.yml logs -f", "block", "watch_mode"),
("docker-compose logs -f", "block", "watch_mode"),
("docker --context x compose logs --follow api", "block", "watch_mode"),
("tail -f app.log", "block", "watch_mode"),
("tail -n 50 app.log", "allow", ""),
("git push origin :main", "block", "force_push"),
("git push --delete origin main", "block", "force_push"),
("git push -d origin main", "block", "force_push"),
("turbo build", "warn", "heavy"),
("vite build", "warn", "heavy"),
("pnpm build:prod", "warn", "heavy"),
("pnpm exec playwright test", "warn", "heavy"),
```
Fixtures: `test/fixtures/yaml-forms/{inline,flow,comment,quoted}/pnpm-workspace.yaml` each with a `packages/a/package.json` (`{"name":"a","scripts":{"test":"vitest"}}`); `test/fixtures/no-config-ws/` = a copy of the pnpm-monorepo layout **without** `hardrail.config.json` (if the existing fixture has none, reuse it and note that in the test).

- [ ] **Step 2: Unit tests** — `test_packages.py`: one `subTest` per yaml form asserting `_workspace_globs(root) == ['packages/*']`; `test_find_workspace_root_from_package_dir`; `test_filter_dot_is_cwd_package`. `test_config.py`: `test_unknown_key_warns` (`{"bogus": 1}` → `_load_error` contains `unknown key bogus`), `test_schema_packages_shape` (schema file's `packages.additionalProperties.properties` keys == `{"test"}`).

- [ ] **Step 3: Verify failure.** — [ ] **Step 4: Implement.** — [ ] **Step 5: Suite + bench** → `OK`, exit 0; also `python3 -m unittest test.adapters.test_claude` (pre-filter sync with the new heavy entry).
- [ ] **Step 6: Commit** — `fix(packages,guard): zero-config workspace root, yaml forms, filter edge cases, broader watch and push rules`

---

### Task 6: Adapter, CI and bench

**Files:**
- Modify: `adapters/claude/pre-bash.sh`, `adapters/claude/pre_bash.py`, `adapters/claude/hooks.snippet.json`, `.github/workflows/ci.yml`
- Test: `test/adapters/test_claude.py`

**Interfaces:**
- `pre-bash.sh`: resolve `$0` through one level of symlink (`[ -L "$0" ]` → `readlink`, relative result joined to `dirname "$0"`); `d=$(cd "$(dirname "$s")" && pwd)`; `[ -f "$d/pre_bash.py" ] || { echo "hardrail: pre_bash.py not found, guard disabled" >&2; exit 0; }`; skip the keyword fast path when `[ -f "${CLAUDE_PROJECT_DIR:-.}/hardrail.config.json" ]`; run python **without** `exec`, then `rc=$?; [ "$rc" -eq 2 ] && exit 2; exit 0` (any other python exit is fail-open; python already printed its own line). Keyword list adds `*turbo*`.
- `pre_bash.py`: `cmd = (event.get("tool_input") or {}).get("command")`; `if not isinstance(cmd, str) or not cmd: sys.exit(0)` — inside the `try`. Still ≤ 30 lines (merge the two stderr branches into one `{"block": ("BLOCKED", 2), "warn": ("WARN", 0)}` lookup).
- `hooks.snippet.json`: add `"timeout": 10` to the hook entry.
- `ci.yml`: `timeout-minutes: 10` on the job; third matrix leg `ubuntu-latest` with `python-version: "3.9"` via `actions/setup-python` pinned to a full SHA (look it up with `git ls-remote https://github.com/actions/setup-python refs/tags/v5*` and record the tag → SHA pair in the commit message); the other legs keep system python. Branch protection on `main` requires `test (ubuntu-latest)` and `test (macos-latest)`; name the new leg so those two names stay unchanged (use `include:` with a `name` field, e.g. `test (ubuntu-latest, py3.9)`).

- [ ] **Step 1: Tests in `test/adapters/test_claude.py`**

```python
def test_symlinked_hook_fails_open_when_shim_missing(self):  # tmp/sym/pre-bash.sh -> real script copy in tmp/real/ without pre_bash.py
    rc, err = run_hook(link, '{"tool_name":"Bash","tool_input":{"command":"pnpm test"},"cwd":"/tmp"}')
    self.assertEqual(rc, 0); self.assertIn('guard disabled', err)
def test_symlinked_hook_still_blocks_when_shim_present(self): ... rc == 2
def test_event_without_command_is_silent(self):
    rc, err = run_hook(HOOK, '{"tool_name":"Edit","tool_input":{"file_path":"x"},"cwd":"/tmp"}')
    self.assertEqual((rc, err), (0, ''))
def test_empty_stdin_is_silent(self): rc == 0 and err == ''
def test_config_present_skips_fast_path(self):  # tmp project with hardrail.config.json {"guard":{"gatedActions":["terraform apply"]}} and CLAUDE_PROJECT_DIR=tmp
    rc, err = run_hook(HOOK, '{"tool_name":"Bash","tool_input":{"command":"ls && terraform apply"},"cwd":"<tmp>"}', env)
    self.assertEqual(rc, 2); self.assertIn('chained_gated', err)
def test_snippet_has_timeout(self): assert json.load(...)["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 10
```
Strengthen `test_prefilter_covers_every_gated_and_heavy_entry`: for each entry build the probe as the entry's **first word only** padded with `x`-free arguments (`"<word> --zz"`), and assert the hook reaches python by checking stderr contains `BLOCKED`/`WARN`/`hardrail:` or the exit is 2 — not by substring over the keyword list.

- [ ] **Step 2: Verify failure.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** `python3 -m unittest test.adapters.test_claude` → `OK`; `wc -l adapters/claude/pre_bash.py` ≤ 30; `python3 test/bench_guard.py` exit 0; `python3 test/test_no_leak.py`-equivalent via unittest → OK.
- [ ] **Step 5: Commit** — `fix(adapter): fail open on missing shim and non-command events, skip fast path with config, pin a python 3.9 CI leg`

---

### Task 7: Robustness test, docs and spec

**Files:**
- Create: `test/core/test_robustness.py`
- Modify: `README.md`, `docs/reference.md`, `docs/overview.md`, `docs/superpowers/specs/2026-10-29-hardrail-design.md`
- Test: `test/test_no_leak.py` (must still pass)

- [ ] **Step 1: Robustness test**

`test_robustness.py`: a seeded generator (`random.Random(1)`) composes 300 commands from fragments (`git push -f`, `pnpm test`, `$(`, `` ` ``, `<<EOF`, `'`, `"`, `|`, `&&`, `\n`, `$'x'`, `env -i`, `{`, `)`, `#`, non-ASCII, `\x00`) and asserts `decide(cmd, '/tmp', DEFAULTS)` returns a `Decision` whose action is in `{'allow','warn','block'}` and never raises, each case under 0.5 s. Run: `python3 -m unittest test.core.test_robustness` → `OK`, total under 10 s.

- [ ] **Step 2: Docs**

- `README.md`: "What it blocks" table gains rows for remote branch deletion, `tail -f`/`docker logs -f`, piped-to-shell and variable command words (as `unparsable`); Configuration says lists replace the defaults; Quick start notes the fast path is skipped when a config file exists; the shim is "30 lines or fewer".
- `docs/reference.md`: import list adds `copy`; fail-open table adds "shim missing / event without a command"; rulings 1–6 and 9 in the rules section; the `pre_bash.py` line count wording.
- `docs/overview.md`: replace "40-row" with "a table test of 180+ rows" only if that count is checked by `python3 -c "from test.core.guard_table import ROWS; print(len(ROWS))"` in the same step, else say "a table test"; add a "Shipped / planned" legend and mark `rules/`, `skills/`, `bin/hardrail`, `templates/`, worktree-guard, metrics, `hardrail.yml` as planned; fix `python3 -I pre_bash.py` → `python3 -I -S`.
- Spec: under "Owner decisions" add the eleven rulings above (one line each, dated 2026-10-29); Verification row for latency says "warm"; the roadmap table gets a `0.1.1 — hardening (this plan)` row; the "Architecture hardening" section notes the unified scanner remains open and lists the three scanners by function name.

- [ ] **Step 3: Verify** — `python3 -m unittest discover -s test -t .` → `OK`; `python3 test/bench_guard.py` → exit 0; `grep -n "40-row" docs README.md` → no output.
- [ ] **Step 4: Commit** — `docs: record hardening rulings, update tables and overview legend; add robustness test`
