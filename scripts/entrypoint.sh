#!/usr/bin/env bash
# Railway / Docker entrypoint: migrate → seed-if-empty → uvicorn.
# Usage: bash scripts/entrypoint.sh   (PORT env optional, defaults to 8000)

set -e

echo "[entrypoint] applying migrations..."
alembic upgrade head

# seed.py is self-idempotent — it sweeps stale agents first, then registers
# only mandates whose mandate_hash isn't already in the DB. Safe to call on
# every boot. Background so Railway healthcheck (30s) sees uvicorn come up
# immediately: full chain register + 31d advance + rebalance/harvest/distribute
# can take 3-5 minutes. Seed progress → /tmp/seed.log (tail via railway logs).
echo "[entrypoint] launching seed in background..."
python -m scripts.seed > /tmp/seed.log 2>&1 &
echo "[entrypoint] seed PID=$! (log: /tmp/seed.log)"

PORT=${PORT:-8000}
echo "[entrypoint] starting uvicorn on 0.0.0.0:$PORT"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
