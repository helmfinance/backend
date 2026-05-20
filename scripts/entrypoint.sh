#!/usr/bin/env bash
# Railway / Docker entrypoint: migrate → seed-if-empty → uvicorn.
# Usage: bash scripts/entrypoint.sh   (PORT env optional, defaults to 8000)

set -e

echo "[entrypoint] applying migrations..."
alembic upgrade head

# Seed only when the DB is empty (idempotent boot — won't clobber prod data).
ROW_COUNT=$(python -c "
from app.db import SessionLocal
from app.db.models import Agent
with SessionLocal() as db:
    print(db.query(Agent).count())
" 2>/dev/null || echo "0")

if [ "$ROW_COUNT" = "0" ]; then
    echo "[entrypoint] empty DB → seeding demo data..."
    python -m scripts.seed || echo "[entrypoint] WARN: seed failed (non-fatal)"
else
    echo "[entrypoint] DB has $ROW_COUNT agents → skip seed"
fi

PORT=${PORT:-8000}
echo "[entrypoint] starting uvicorn on 0.0.0.0:$PORT"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
