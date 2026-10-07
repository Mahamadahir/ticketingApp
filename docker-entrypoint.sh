#!/bin/sh
set -e

python -m app.check_settings

# Migrations run once here rather than in app startup, where every uvicorn
# worker would race to apply them.
alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-4}"
