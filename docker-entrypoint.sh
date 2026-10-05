#!/bin/sh
set -e

# Migrations run once here rather than in app startup, where every uvicorn
# worker would race to apply them.
alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
