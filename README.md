# Collicity

AI agents that act for a named person, inside that person's own access, with every side effect
approved. The product specification and technical design are in [docs/](docs/).

## Layout

The repository follows the technical design (§13). Only the parts built so far exist:

```text
python/                uv workspace (Python 3.13)
  libs/core            identifiers, tenant context, log redaction (SEC-03)
  libs/db              database roles, tenant-scoped sessions (SEC-09), models, migrations
  libs/ledger          price catalog, worst-case cost estimates, budget reservations (BUD-*)
  services/app         the app service: API for people (FastAPI)
  testing/             pytest plugin: migrated Postgres fixture; fakes and stubs later
catalog/prices/        versioned price catalog files, published with `just prices`
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

## Issue tracking

Work is tracked with [beads](https://github.com/gastownhall/beads): `bd ready` lists what's
unblocked.
