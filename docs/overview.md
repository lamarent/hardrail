# hardrail — how the pieces fit

```
                 hardrail repo (public)
                 ├── core/      decide() · mapping · render · metrics schema   ← 40-row table tests
                 ├── adapters/  claude/ · none/ · (codex/)      ≤30-line shims
                 ├── templates/ rules · skills · agents · workflows
                 └── bin/hardrail  init | check | upgrade | render | ci | metrics

 ┌─ 1. ADOPT ───────────────────────────────────────────────────────────────────┐
 │  hardrail init                                                               │
 │    detect lockfile · package.json · workspaces · biome/eslint · lefthook     │
 │    → confirmation table (packages, test cmd, rule byte cost, modules)        │
 │    → hardrail.config.json  (the one project-specific file)                   │
 │    → .claude/hardrail/{core,pre-bash.sh}  rules/hardrail/  skills/hardrail-* │
 │    → .github/workflows/hardrail.yml       .hardrail-manifest.json            │
 │    existing file? → <name>.hardrail-new, never overwritten                   │
 │    smoke: effective settings has hook? → feed `pnpm test` → BLOCKED          │
 └──────────────────────────────────────────────────────────────────────────────┘
                                   │ committed → every teammate gets it by clone
                                   ▼
 ┌─ 2. RUN (per session) ───────────────────────────────────────────────────────┐
 │  prompt ──► Claude Code ──► Bash tool call                                   │
 │                │                 │ PreToolUse                                 │
 │   rules/ (prose tier)      pre-bash.sh  fast path: no gated keyword → exit 0 │
 │   skills/ on description        │ else python3 -I pre_bash.py               │
 │                                 ▼                                             │
 │                        core.decide(cmd, cwd, config)                          │
 │                        unparsable → force_push → chained → watch →            │
 │                        unscoped_test → heavy                                  │
 │                                 │                                             │
 │              allow: exit 0      warn: stderr WARN, exit 0                     │
 │              block: stderr "BLOCKED [rule]: <fixed command>", exit 2          │
 │                                 │ agent retries with the fixed command        │
 │  Edit/Write ──► worktree-guard: main checkout? → BLOCKED, print worktree cmd  │
 └──────────────────────────────────────────────────────────────────────────────┘
                                   │ git push → PR
                                   ▼
 ┌─ 3. CI (no agent needed) ────────────────────────────────────────────────────┐
 │  hardrail.yml (config read from base branch)                                 │
 │   scoped-tests: diff --name-status -M → only .ts A/M & not alwaysFull?       │
 │                 yes → vitest related <files>   no → full suite + dependents   │
 │   typecheck (always) · codegen-in-sync · conventions · full-suite on main    │
 │   self-check: hardrail check (schema, manifest drift, hooks active)          │
 └──────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
 ┌─ 4. IMPROVE ─────────────────────────────────────────────────────────────────┐
 │  hardrail metrics blocks  ← ~/.claude/projects transcripts (+subagents)      │
 │     counts only: blocks by rule, denials, compactions, interrupts            │
 │  human reads report → promote rule (prose→hook→ci) or drop a hook            │
 │     that never fires → edit config / templates → hardrail render → commit    │
 │  Claude Code update → nightly contract test on real CLI → fix adapter only   │
 └──────────────────────────────────────────────────────────────────────────────┘
```
