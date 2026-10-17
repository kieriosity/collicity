# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

```bash
just sync     # uv workspace in python/ (Python 3.13)
just dev      # local Postgres 17 on 127.0.0.1:55432, roles, migrations
just check    # ruff, pyright (strict on src), pytest — what CI runs
just test libs/db -k isolation   # extra args go to pytest
```

Use 127.0.0.1, not localhost, in local database URLs: on Windows an IPv6 attempt stalls each
connection.

## Architecture Overview

The source of truth is `docs/Collicity — Technical Design (Draft v0.5).md` (and the spec it
accompanies). Requirement IDs (SEC-09, RUN-07, …) refer to the spec; cite them in docstrings and
test names. Three services (`app`, `worker`, `gateway`) and an `extract` Lambda; only the
gateway holds credentials or reaches the internet. Postgres is the system of record.

## Conventions & Patterns

- Invariants hold by structure: enforce them with database roles, RLS, IAM or a single code
  path, and prove them with tests that read the catalog or try the forbidden thing.
- Tenant data is touched only inside `collicity_db.tenant_session()` / `tenant_connection()`.
- Migrations are hand-written SQL in `python/libs/db/src/collicity_db/migrations/versions/`,
  forward-only. Every tenant table: `ENABLE` + `FORCE` RLS with the `tenant_isolation` policy,
  composite `(tenant_id, …)` foreign keys, and explicit grants mirrored in
  `libs/db/tests/test_roles.py::EXPECTED_TABLE_GRANTS`. Keep `collicity_db.models` in step.
- State that may change only in controlled ways (balances now; approvals and audit later)
  changes through `SECURITY DEFINER` functions owned by a NOLOGIN role from
  `collicity_db.roles.DefinerRole`, in that role's own schema, with
  `SET search_path = pg_catalog, pg_temp`, and still bound by tenant RLS. Grant `EXECUTE` per
  service role, never to PUBLIC; `test_roles.py` lists every grant.
- Audit through `collicity_audit.append` in the same transaction as the work. `detail` holds
  identifiers and hashes only; anything about a person goes in `personal`, which is encrypted.
- Money is integer micro-USD. Prices come only from `catalog/prices/*.json`, published as
  immutable versions; never hard-code a price.
- Prove a concurrency claim with a test that forces the interleaving (see
  `libs/ledger/tests/test_concurrency.py`), then check that it fails with the protection removed.
- Check that a property test isn't vacuous: break the code it guards and confirm it fails
  (back up the files first, untracked ones included). See `libs/grants/tests/test_properties.py`.
- Connector calls are decided only by `collicity_grants.evaluate`; widening a grant element means
  updating `widens` and its tests together. Regenerate `schemas/` with `just schemas`.
- Files are UTF-8. Some editors on this machine write UTF-16; check with `file` if unsure.
- Never edit the spec or technical design in place; new drafts go in `docs/`, old ones to
  `docs/archive/`.
