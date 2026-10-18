# Collicity

AI agents that act for a named person, inside that person's own access, with every side effect
approved. The product specification and technical design are in [docs/](docs/).

## Layout

The repository follows the technical design (§13). Only the parts built so far exist:

```text
python/                uv workspace (Python 3.13)
  libs/core            identifiers, tenant context, log redaction (SEC-03)
  libs/assignments     assignments, immutable versions, acceptance, step grants, kill switch
  libs/audit           write-ahead, hash-chained audit log with crypto-shredding (ADM-07, SEC-07)
  libs/db              database roles, tenant-scoped sessions (SEC-09), models, migrations
  libs/grants          the permission ceiling: manifests, capability grants, evaluator (CON-02)
  libs/ledger          price catalog, worst-case cost estimates, budget reservations (BUD-*)
  libs/runs            run admission, state transitions, run kill switch, outbox relay
  services/app         the app service: API for people (FastAPI)
  testing/             pytest plugin: migrated Postgres fixture; fakes and stubs later
catalog/prices/        versioned price catalog files, published with `just prices`
schemas/               JSON Schemas for manifests, grants and admin policy (`just schemas`)
compose.yaml           local Postgres 17
justfile               developer tasks
docs/                  specification, technical design, archive
```

## Getting started

You need [uv](https://docs.astral.sh/uv/), [just](https://just.systems/) and Docker.

```bash
just sync          # install the Python workspace
just dev           # start Postgres on 127.0.0.1:55432, create roles, migrate, publish prices
just app           # serve the API on http://localhost:8000 (/docs, /readyz)
just check         # lint, types and tests, as CI runs them
```

Tests start their own Postgres container. To reuse the one from `just dev`, run them with
`COLLICITY_TEST_ADMIN_DATABASE_URL=postgresql+psycopg://postgres:postgres-local-only@127.0.0.1:55432/postgres just test`.

## Database model in one paragraph

`migrator` owns every table and runs only in CI and deploys. Each service connects as its own
role (`app_user`, `app_worker`, `gw`) that owns nothing and can't bypass row-level security.
Every tenant table forces RLS on `tenant_id = current_tenant_id()`, and services set the tenant
and acting person per transaction through `collicity_db.tenant_session()`. With no tenant set,
tables read as empty. Migrations are hand-written SQL and forward-only. The tests in
`python/libs/db/tests/test_roles.py` read the catalog, so a new table without forced RLS, or a
new grant to a service role, fails CI until the expected list is updated in the same change.

## Budgets in one paragraph

Every billable operation reserves its worst case before it starts (BUD-03). `ledger.reserve`
holds the amount against every organization budget, the person's user budgets and the
assignment or workspace budget in one transaction, or holds nothing (BUD-10). The gateway
recomputes the worst case from the reservation's price version and refuses a smaller hold before
it marks the reservation started; afterwards it settles the actual cost. Balances change only
through those `SECURITY DEFINER` functions, which tenant RLS still binds, and grants split them
by role: app and worker reserve, only the gateway starts and settles. Prices are integer
micro-USD, come only from published, immutable catalog versions (BUD-08), and each reservation
records the version it used.

## Grants in one paragraph

Before the gateway releases any credential, `collicity_grants.evaluate` checks the call against
the permission ceiling (CON-02): the connector and action, the resources (stable IDs resolved at
acceptance), the fields written, the recipients, parameter limits and the per-run volume, plus
the admin's policy and the person's consented scopes. It denies by default and returns the first
reason, refuses security changes outright (SEC-16) and any action without a cost model (BUD-03),
and escapes text so it can't become a spreadsheet formula. `filter_response` drops what a read
returns outside the grant, and `widens` lists every way a new version grants more, which is what
triggers re-acceptance (RUN-03).

## Audit in one paragraph

Every service writes audit records only through `audit.append`, in the same transaction as the
work they describe (write-ahead), and nobody, migrator included, can update or delete one. A
sealer links each tenant's records into a hash chain, and `audit.verify` recomputes every digest
and link, so an edited, removed, reordered or truncated record shows. Records hold identifiers
and hashes; personal fields are encrypted in the application under a per-person key, so
destroying that key (crypto-shredding, SEC-07) erases them while the chain still verifies.
Searchable personal fields get keyed-hash blind indexes outside the chain. Signed anchors in S3
Object Lock, which catch a forger who rewrites the whole chain, come with the AWS work.

## Versions and step grants in one paragraph

An assignment's versions are immutable, and only `standing.accept_version`, run by the
assignment's owner, makes one current; the worker can't change standing instructions at all
(SEC-12). Each run step gets a step grant: a row naming the capabilities it may use, which the
worker issues only from an accepted version. The database stamps it with the tenant's, the
owner's and the assignment's current epochs. The gateway checks it before every call, and a
pause (ASG-13) bumps the paused scope's epoch, so every earlier grant fails at once and a resume
never revives it. Pauses are audited and announced on `collicity_epochs` for gateway caches.

## Runs in one paragraph

Postgres owns every run's state. Only admission creates runs (RUN-09): it drops duplicate
triggers (RUN-02), applies the assignment's concurrency policy and starts runs that carry a
decided approval first (RUN-01), and parks a run until a version is accepted (RUN-03). A run's
state changes only through `runtime.transition`, which checks its version, allows only the §7.6
moves, and refuses an outcome that misstates what the run did: no Cancelled or Failed after a
side effect, no Rolled back without one (RUN-08). Every change writes outbox events in the same
transaction; the relay numbers each tenant's events when it publishes them, so a late commit is
never skipped, and delivers them at least once. Entering Running starts a new segment, which the
Temporal worker (next) executes until the run next waits.

## Issue tracking

Work is tracked with [beads](https://github.com/gastownhall/beads): `bd ready` lists what's
unblocked.
