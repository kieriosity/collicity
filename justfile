# Collicity developer tasks. Run `just` to list them.

set dotenv-load := true

py := "uv run --directory python"

# Local-only credentials; every one can be overridden from the environment or a .env file.
export COLLICITY_ADMIN_DATABASE_URL := env("COLLICITY_ADMIN_DATABASE_URL", "postgresql+psycopg://postgres:postgres-local-only@127.0.0.1:55432/postgres")
export COLLICITY_ROLE_PASSWORD_MIGRATOR := env("COLLICITY_ROLE_PASSWORD_MIGRATOR", "migrator-local-only")
export COLLICITY_ROLE_PASSWORD_APP_USER := env("COLLICITY_ROLE_PASSWORD_APP_USER", "app-user-local-only")
export COLLICITY_ROLE_PASSWORD_APP_WORKER := env("COLLICITY_ROLE_PASSWORD_APP_WORKER", "app-worker-local-only")
export COLLICITY_ROLE_PASSWORD_GW := env("COLLICITY_ROLE_PASSWORD_GW", "gw-local-only")
export COLLICITY_MIGRATOR_DATABASE_URL := env("COLLICITY_MIGRATOR_DATABASE_URL", "postgresql+psycopg://migrator:" + COLLICITY_ROLE_PASSWORD_MIGRATOR + "@127.0.0.1:55432/collicity")
export COLLICITY_APP_DATABASE_URL := env("COLLICITY_APP_DATABASE_URL", "postgresql+psycopg://app_user:" + COLLICITY_ROLE_PASSWORD_APP_USER + "@127.0.0.1:55432/collicity")

default:
    @just --list

# Install the Python workspace
sync:
    uv sync --directory python

# Start local services, create roles and migrate
dev: db-up db-migrate

# Start Postgres and wait until it's healthy
db-up:
    docker compose up -d --wait postgres

# Create roles and the database (idempotent), then apply migrations as migrator
db-migrate:
    {{py}} python -m collicity_db bootstrap
    {{py}} python -m collicity_db upgrade

# Stop local services; `just db-down -v` also deletes the data
db-down *args:
    docker compose down {{args}}

# Serve the app API on http://localhost:8000
app:
    {{py}} uvicorn --factory collicity_app.main:create_app --reload --port 8000

# Run tests; extra arguments go to pytest
test *args:
    {{py}} pytest {{args}}

# Lint, check formatting and type-check
lint:
    {{py}} ruff check .
    {{py}} ruff format --check .
    {{py}} pyright

# Fix lint findings and format
fmt:
    {{py}} ruff check --fix .
    {{py}} ruff format .

# Everything CI runs
check: lint test
