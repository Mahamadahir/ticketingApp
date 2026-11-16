#!/bin/sh
set -e

python -m app.check_settings

# Migrations run once here rather than in app startup, where every uvicorn
# worker would race to apply them. In Azure RUN_MIGRATIONS=false: the migrate
# job is the only thing that migrates, so replicas never race each other.
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    alembic upgrade head
fi

# Local development only (docker-compose.override.yml sets it): one process that restarts when a
# file in app/ changes (uvicorn cannot reload with --workers, so UVICORN_WORKERS is ignored). Unset, the line below is unchanged.
if [ "${UVICORN_RELOAD:-false}" = "true" ]; then
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-4}"
