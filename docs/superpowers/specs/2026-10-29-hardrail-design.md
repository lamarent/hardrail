# hardrail — design

Date: 2026-10-29. Status: approved by owner after four independent reviews
(Claude Code mechanics, scope, hook/CI safety, hiring-reader).

## Intent

A Claude Code harness that a TypeScript repository adopts in one command and that the
whole team inherits by cloning the repo. It enforces rules by machine where prose has
failed, and it counts its own enforcement so the team promotes or drops rules on its own
data.

Extracted from a private multi-repo workspace where the same mechanisms ran for several
months. Only the mechanisms travel; nothing project-bound does.

## Why

- Rules written as prose are still violated, most often on exactly the commands the prose
  spells out (unscoped test runs, watch-mode runners, chained state-changing commands).
  A PreToolUse hook that blocks the command and prints the corrected form removes the
  option instead of lowering the odds. True by construction, not by measurement.
- Setting up Claude Code for a repository is repeated by hand, and every lesson is
  re-learned from scratch. A config-driven `init` carries the lessons.
- Nothing counts which rule is violated, so promotions (prose → hook → CI) are decided by
  feel. The harness ships the counter.

No numbers from the source workspace are published. The README shows how to produce the
same report on the adopter's own transcripts.

## Goals

1. One command adopts it in a new or existing TS repo; no hand-edited paths.
2. Every project-specific fact lives in `hardrail.config.json`; everything else reads it.
3. Runtime never depends on the plugin: hooks, rules and CI live in the adopting repo.
4. Each rule is enforced at the lowest tier that actually holds: prose → hook → CI.
5. `init` never modifies a file the repo already had.
6. Every claim in the README is backed by a test in this repo or a command the reader runs.
7. The core knows no agent. Claude Code is the first adapter, not the design.

## Non-goals

- Replacing the project's lint / test / CI stack. The harness wraps and scopes them.
- Non-TS stacks in v1 (config leaves a slot).
- Shipping memories, transcripts, ticket ids, customer names or infrastructure notes.
- An agent that edits its own rules; it proposes, a human commits.
- Correction-heuristic metrics, `review`/`label` promotion proposals: v1.1.
- Flaky-test and CI-duration analytics: out of scope.

## Anti-goals

- A fork of the source workspace with names replaced. If a reader can tell which company
  it came from, it failed.
- Rules copied but never enforced or counted. Every rule states its tier and the generic
  incident that put it there.
- A hook that is "installed but not active" without anyone noticing.

## Constraints

- Hooks: python3 stdlib only, self-contained files copied into the adopting repo; a bash
  fast path exits early for commands that contain no gated keyword. No `jq`, no Node, no
  import from the plugin directory.
- Hook p95 latency < 100 ms, measured in this repo's CI, cold and warm.
- Minimum Claude Code version pinned in the manifest (the version where plugin `bin/`,
  `${CLAUDE_PLUGIN_DATA}`, `PostCompact` and `.worktreeinclude` all exist).
- One language: python3 ≥ 3.9 stdlib for core, adapters and `bin/hardrail`. No Node
  anywhere in this repo; adopters' Node tooling is only invoked, never required to run us.
- Licence MIT. Public repo `hardrail`; plugin name `hardrail` (names starting
  with `claude-` are reserved).
- macOS and Linux. Windows out of v1.

## Distribution: repo first, plugin second

| Component | Where it lives in the adopting repo | Who gets it |
|---|---|---|
| Hooks | `.claude/settings.json` → `hooks` → `.claude/hardrail/hooks/*.py` | everyone who clones |
| Rules, skills, agents | `.claude/rules/hardrail/`, `.claude/skills/hardrail-*/`, `.claude/agents/` | everyone |
| Config | `hardrail.config.json` at repo root | everyone, and CI |
| CI | `.github/workflows/hardrail.yml` | everyone, Claude user or not |
| Manifest | `.claude/.hardrail-manifest.json` (version + content hash per installed file) | `init --check` |
| Plugin reference | `.claude/settings.json` → `extraKnownMarketplaces`, `enabledPlugins` | Claude Code prompts to install |

The plugin `hardrail` provides only `hardrail init | check | upgrade` and the fixture
tooling. If it is absent, every guard still runs. Whether project-scope `enabledPlugins`
prompts teammates to install is verified during the build; if it does not, teammates use
`npx` from the hardrail repo for `upgrade` only.

Existing `.claude/settings.json`: `init` prints the `hooks` snippet and does not edit the
file. `init --check` (also the CI self-check) fails until the hooks are registered, so an
inactive guard cannot persist silently.

## Core and adapters

| Layer | Contents | Agent-specific |
|---|---|---|
| Core | `hardrail.config.json`; guard decision `decide(command, cwd, config) → allow \| warn \| block + message`; source → test mapping; CI workflow; convention check; rule **content**; metrics schema | no |
| Adapter `claude` (v1) | registers hooks in `.claude/settings.json`; reads Claude's stdin JSON; exit 2 + stderr; renders rules into `.claude/rules/`, skills into `.claude/skills/`; reads `~/.claude/projects` transcripts | yes |
| Adapter `codex` (later) | renders rules into `AGENTS.md`; plugs `decide` into Codex's pre-command hook if one exists (to verify at build time), otherwise pre-commit only; reads Codex session logs | yes |
| Adapter `none` | guard as git `pre-commit`; rules as `AGENTS.md`; CI unchanged | no |

Each adapter is a thin shim (target ≤ 30 lines) that translates input and output; it
holds no policy. The 40-command guard table and the mapping matrix test the core, so a
new adapter adds no guard tests. `hardrail init --adapter claude` selects the shim;
`init` detects the agent from the repo (`.claude/`, `AGENTS.md`, `.codex/`) and proposes.

## The parts

### 1. `hardrail.config.json`

```jsonc
{
  "defaults": { "test": "pnpm exec vitest --run", "lint": "biome check", "typecheck": "pnpm typecheck" },
  "packages": {
    "backend":  { "root": "packages/backend",  "testFor": "{dir}/{name}.test.{ext}" },
    "frontend": { "root": "packages/frontend", "testFor": ["{dir}/{name}.test.{ext}", "{dir}/__tests__/{name}.test.{ext}"] }
  },
  "guard": { "mode": "block", "gatedActions": ["git commit", "git push", "rm -rf", "docker", "pnpm install", "prisma migrate", "gh pr"] },
  "heavy": ["build", "generate:*", "prisma *", "playwright", "docker compose logs"],
  "worktree": { "required": true, "dir": ".worktrees", "include": [".env*", "certs/", "lefthook-local.yml"] },
  "ci": { "alwaysFull": ["**/handlers/**", "**/di/**"], "skip": ["**/*.md", "docs/**"], "base": "main" },
  "codegen": [{ "run": "pnpm generate:openapi", "verify": "git diff --exit-code -- openapi/" }],
  "conventionChecks": [],
  "modules": { "cleanup": true, "board": false, "standup": false, "sessionResume": false, "codeReview": false, "archTest": false },
  "language": { "reply": "en", "published": "en" }
}
```

A JSON schema ships; `init --check` validates. `packageManager` is detected from the
lockfile. `heavy` and `alwaysFull` are globs matched against the normalised command /
path after the package-manager prefix is stripped. Hooks and CI read the config at
runtime; rules are rendered from it, and `--check` reports rendered rules that are stale.

### 2. Hooks (machine tier)

| Hook | Event | Behaviour |
|---|---|---|
| `bash-guard` | PreToolUse(Bash) | tokenise with `shlex`; split on `; & && \|\| \| newline`; treat `bash/sh/zsh -c`, stdin heredocs and `eval` as payloads scanned on their own; runner list = config `test` commands + builtin (`npm/pnpm/yarn/bun test`, `vitest`, `jest`, `turbo run test`, `nx test`, `node_modules/.bin/*`); require a path argument in the **same simple command**; scope roots from `packages[].root`, not a hard-coded `src/`; accept `--run`, `vitest run`, `--watch=false`, `--no-watch`, `CI=1`; gated actions may not be chained with anything; force push = `--force`, combined short flags containing `f`, `+refspec`, after git global options (`-C`, `-c`); `--force-with-lease` allowed; resolve package from `-C`/`--filter` (name → root via workspace `package.json`), then cwd; unparsable input (unbalanced quotes) → block in `block` mode, warn in `warn` mode, never silent allow; prints the corrected command on **stderr** |
| `worktree-guard` | PreToolUse(Edit\|Write) and inside `bash-guard` | when `worktree.required`, block writes and `git commit/push` whose target is the main checkout; print the worktree command |
| `convention-check` | pre-commit (lefthook/husky if present) and CI, not on every Edit | runs `conventionChecks` |
| `compaction-count` | PostCompact | appends one line to the local metrics store |

All hooks fail open with a one-line stderr warning if python3 is missing; `init --check`
verifies python3 and hook paths. Missing `jq`/Node cannot matter because none is used.

### 3. Rules (prose tier, always loaded)

`verification.md`, `heavy-commands.md`, `context-budget.md`, `requirement-first.md`.
Each ≤ 60 lines, rendered from config, with **Tier** (`prose` / `hook` / `ci`) and a
generic **Incident**. `cleanup.md` moves to the `cleanup` module; communication becomes
the `language` config key and one line in the rendered CLAUDE.md snippet.

### 4. Skills and agents

Core skills: `tdd`, `done-checklist`, `worktree-setup` (uses `.worktreeinclude` and
`worktree.include`), `pr-creation`, `codegen-chain`. No skill router: Claude Code selects
skills from `description`. Agent `runner` (one heavy command → exit code + counts).

Optional modules, enabled per `modules.*` at `init` and each stating its prerequisites:
`cleanup` (enumerate leftovers by command, never delete), `board` + `TASKS.md`,
`standup`, `sessionResume` (reads whatever store exists), `codeReview` (needs `gh`;
posting gated behind confirmation), `archTest` (a ~60-line stdlib import-graph layer rule,
e.g. `routes → usecases → repositories`, wired into `conventionChecks`).

### 5. `init`

```
hardrail init              # detect → confirmation table → install → smoke → manifest
hardrail init --check      # schema, manifest drift, hooks active, python3 present, stale renders
hardrail init --upgrade    # untouched files updated in place; edited files get .hardrail-new
hardrail init --render     # re-render rules after a config edit
```

Detection reads lockfile, `package.json`, `pnpm-workspace.yaml` (minimal `packages:`
parser) / `workspaces`, `biome.json` / `.eslintrc*`, `lefthook.yml` / `.husky/`,
`prisma/`, `.github/workflows/*`, `.worktrees/`, `.worktreeinclude`. It prints the table,
including each rule's byte cost, and stops for confirmation.

Install rules: a path the repo lacks → written; a path that exists → `<name>.hardrail-new`
beside it, listed at the end; the repo's own `agents/`, `commands/`, `skills/` untouched;
`.github/workflows/` follows the same rule; the generated workflow is non-required at
first and says so; `.gitignore` is never edited — new directories carry their own
`.gitignore` (`.worktrees/.gitignore`, metrics store outside the repo under
`${CLAUDE_PLUGIN_DATA}` or `~/.hardrail/<repo-id>/`). Second run is idempotent via
the manifest. Plugin and repo hooks both present → `init` refuses, to avoid double firing.

Smoke test reads the **effective** settings (`settings.json`, `settings.local.json`) and
confirms the hook entries exist, then feeds `<pm> test` through the registered hook.
Otherwise it prints `INSTALLED BUT NOT ACTIVE`. Backfill of metrics asks first, lists the
matched transcript slugs (worktrees have their own), reports the `cleanupPeriodDays`
window and never changes it.

### 6. CI layer

`hardrail ci` writes `.github/workflows/hardrail.yml`:

- **scoped-tests**: `fetch-depth: 0`; diff `git merge-base origin/<base> HEAD` with
  `--name-status -M`; narrow only when every changed file is a `.ts/.tsx` source or test
  with status A/M and matches no `alwaysFull` glob; selection by `vitest related --run
  <files>` (import graph), never by name convention alone; any D/R, any other file type,
  any root-level file, or an empty/erroring diff with code changes → full suite of every
  affected package and its `workspace:` dependents; never `--passWithNoTests`; assert at
  least one test ran when a narrow set was chosen.
- **typecheck**: always, for every affected package (vitest strips types).
- **full-suite**: on push to base branch, plus nightly.
- **codegen-in-sync**, **conventions**, **self-check** (`init --check`, `claude plugin
  validate --strict` when the plugin is referenced).
- Hardening: `pull_request` only, never `pull_request_target`; `permissions: contents:
  read`; actions pinned by SHA; `hardrail.config.json` read from the base branch; no
  `paths:` filter on a required job.

### 7. `metrics blocks`

Reads transcripts (main session **and** `subagents/`), counts hook blocks by kind, denials
by `toolDenialKind`, compactions via `isCompactSummary`, interrupts via the interrupt
record (content may be a list of blocks). Stores counts and enum kinds only; no prompt
text, no commands, no absolute paths; session ids hashed. `report [--days N] [--split
DATE]` prints per-session rates. Everything else in measurement is v1.1.

### 8. Catalog

`docs/catalog/<pattern>.md`: Symptom · generic example · tier that holds it · the hook or
metric that counts it · what lower tiers tried. v1 ships one complete entry (unscoped
tests); others follow with the same template. No counts.

## Claude Code compatibility

### Mechanisms used (nothing patched)

| hardrail part | Claude Code mechanism | Verified 2026-10-29 |
|---|---|---|
| `bash-guard` | `PreToolUse` hook, matcher `Bash`, registered in `.claude/settings.json`; exit 2 blocks; the agent reads **stderr** only | yes |
| `worktree-guard` | `PreToolUse` hook, matcher `Edit\|Write`; `$CLAUDE_PROJECT_DIR` names the main checkout | yes |
| guard-rail rules | `.claude/rules/*.md` without `paths:` load at launch like CLAUDE.md | yes |
| skills | `.claude/skills/<name>/SKILL.md`, selected from `description` | yes |
| agent `runner` | `.claude/agents/runner.md` with `model`, `tools` | plugin agents honour a subset of frontmatter — verify at build |
| `metrics blocks` | transcripts in `~/.claude/projects/<slug>/` (+ `subagents/`); `isCompactSummary`, `toolDenialKind`, `hook error:` lines; `CLAUDE_CONFIG_DIR` moves the tree | yes, on real transcripts |
| `compaction-count` | `PostCompact` hook | yes |
| worktree | `.worktreeinclude` copies git-ignored files into new worktrees | yes |
| team distribution | `.claude/settings.json` committed → every clone has the hooks; `extraKnownMarketplaces` + `enabledPlugins` prompt plugin install | project-scope prompt **not verified** |
| plugin | `.claude-plugin/plugin.json`, `hooks/hooks.json`, `bin/` on PATH, `${CLAUDE_PLUGIN_DATA}`; names starting `claude-` reserved | yes |
| CI | none; the workflow reads `hardrail.config.json` | independent |

Not provided by Claude Code, so hardrail does it: a plugin cannot inject always-loaded
rules (`init` copies them into the repo); `permissions.deny` matches prefixes only, so it
cannot replace a parsed guard.

### Staying compatible across Claude Code updates

1. Everything Claude-specific lives in the `claude` adapter (≤ 30 lines). A format change
   touches the shim, never core or its tests.
2. **Contract test on the real CLI, nightly**: CI installs the latest `claude`, runs it
   headless on a fixture repo with the hook registered, and asserts `BLOCKED` on stderr
   for `pnpm test`. A breaking release is known within a day.
3. **Versioned fixtures**: `test/fixtures/claude/<version>/` holds real PreToolUse stdin
   JSON and transcript excerpts per tested version; adapter and metrics tests run on all.
4. `hardrail check` compares `claude --version` with `minVersion` / `testedUpTo` in the
   manifest; above `testedUpTo` warns, never blocks.
5. **Loud fail-open**: unparsable hook input → stderr `hardrail: unexpected hook input,
   guard disabled`, exit 0. CI remains the backstop.
6. `claude plugin validate --strict` in CI for the manifest.

## Verification (what this repo's CI proves)

| Part | Test | Pass criterion |
|---|---|---|
| bash-guard | table of ≥ 40 commands incl. `p"n"pm test`, `npm test`, newline/`&` chains, heredoc into `bash`, `-C`/`--filter`, `tests/unit/`, `find -exec rm {} \;`, `docker run sh -c "a; b"` | every row as expected |
| latency | 50 runs, cold + warm | p95 < 100 ms |
| worktree-guard | fixture with `.worktrees/`; edit in main vs worktree; warn mode | blocks only in main; warn never blocks |
| init no-overwrite | fixture with existing `.claude/` + `settings.json`; hash all files before/after; run twice | hashes unchanged; `.hardrail-new` present; second run writes nothing |
| init --check | edit an installed file; remove a hook entry | non-zero, names the file / the hook |
| scoped-tests mapping | 2-package fixture; matrix: has test, no test, rename, delete, barrel, shared type, lockfile, dependent package | correct set per case; never 0 tests on a code change |
| generated workflow | `actionlint`; run on fixture (`act` or this repo's Actions) | green on clean fixture, red on a planted violation |
| rule render | snapshot from sample config | no placeholder left |
| metrics blocks | synthetic transcripts with known counts incl. subagent dir | exact counts |
| name leak | local pre-push denylist (not shipped) + CI regex (ticket ids, `/Users/`, `/home/`, emails, internal domains) | zero hits before first push |
| contract (nightly) | latest `claude` CLI headless on fixture with hook registered | `pnpm test` → `BLOCKED` on stderr |
| acceptance | a public TS repo the owner never worked in: `init` → smoke → `pnpm test` blocked → PR with a code change | hook blocks, CI picks the right tests, no repo file modified |

README evidence tiers: **verified by this repo's CI** and **reproducible by you**. No
third tier.

## Build order

1. `bash-guard` + its table tests + latency benchmark, against fixtures.
2. `init` install logic + no-overwrite and idempotency tests, before any rule text.
3. Shared source → test mapping module, then the CI job and its matrix.
4. Rule templating and render snapshot.
5. `worktree-guard`, `worktree-setup`.
6. `metrics blocks`.
7. Name-leak gate; first push.
8. Acceptance on the stranger repo; then README and the first catalog entry.
9. Optional modules, in the order the owner uses them most.

## Repo layout

```
hardrail/
  .claude-plugin/plugin.json      # name: hardrail
  bin/hardrail                    # init | check | upgrade | render | ci | metrics (python3 stdlib, no deps)
  core/                           # decide(), mapping, render, metrics schema (python3 stdlib)
  adapters/claude/ adapters/none/ # thin shims copied into adopters
  templates/rules/ templates/skills/ templates/agents/ templates/workflows/
  modules/<name>/                 # optional modules
  schema/hardrail.config.schema.json
  test/fixtures/ test/guard/ test/init/ test/ci/
  docs/catalog/ docs/superpowers/specs/
  README.md                       # quick start → mechanism → verify it yourself
```

## Left to the builder

- Exact schema, placeholder syntax, manifest format, label of the metrics store.
- Fixture repo contents.
- README structure beyond its ordering.

## Owner decisions

- Workspace is a reference; nothing project-bound ships. No published numbers.
- Public repo; no repository, company, product, ticket or person named anywhere.
- Both a chat correction and a hook denial count as a violation (v1.1 uses both).
- CI layer in v1. Worktree is required when working with Claude Code, so it is a guard.
- Core vs optional decided by "works on a real repo", not by job-description fit.
- Agent proposes; human approves.
