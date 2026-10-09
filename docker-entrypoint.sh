#!/bin/sh
set -e

python -m app.check_settings

# Migrations run once here rather than in app startup, where every uvicorn
# worker would race to apply them. In Azure RUN_MIGRATIONS=false: the migrate
# job is the only thing that migrates, so replicas never race each other.
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    alembic upgrade head
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-4}"
