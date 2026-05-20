#!/usr/bin/env bash
# Reset demo state: wipe DB + IPFS stub + restart uvicorn with fresh seed.
# Usage: bash scripts/reset_demo.sh

source .venv/bin/activate 2>/dev/null || true

set -e

cd "$(dirname "$0")/.."

echo "🧹 Stopping uvicorn..."
pkill -f 'uvicorn app.main' 2>/dev/null || true
sleep 2

echo "🗑️  Wiping DB and IPFS stub..."
rm -f helm.db
rm -rf data/mandates
mkdir -p data/mandates

echo "📦 Applying migrations..."
alembic upgrade head

echo "🌱 Seeding demo data..."
python -m scripts.seed

echo "🚀 Starting uvicorn (background)..."
uvicorn app.main:app --port 8000 > /tmp/uvicorn.log 2>&1 &
UVICORN_PID=$!
echo "   PID: $UVICORN_PID"

echo "⏳ Waiting 10s for indexer first cycle..."
sleep 10

echo ""
echo "✅ Demo state ready."
echo ""
echo "Endpoints:"
echo "  - /system/info: http://localhost:8000/system/info"
echo "  - /agents:      http://localhost:8000/agents"
echo "  - /docs:        http://localhost:8000/docs"
echo ""
echo "Logs: tail -f /tmp/uvicorn.log"
