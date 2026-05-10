# Helm Backend

FastAPI + Pydantic v2 + web3.py + Anthropic SDK.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in values
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

| Endpoint | Purpose |
|---|---|
| http://localhost:8000 | API root (404 — no index handler) |
| http://localhost:8000/openapi.json | Raw OpenAPI 3.1 document |
| http://localhost:8000/docs | Interactive Swagger UI |
| http://localhost:8000/redoc | Alternate doc viewer |

## Project layout

```
app/
├── main.py                  # FastAPI entrypoint, route registrations
├── schemas.py               # ★ Pydantic models — single source of truth
├── core/                    # config, settings, deps, security
├── api/                     # route handlers (split when main.py grows)
│   ├── agents.py
│   ├── mandate.py
│   ├── portfolio.py
│   └── system.py
├── agents/                  # agent runtime
│   ├── decision.py          # deterministic rebalance logic
│   ├── narrator.py          # Claude weekly notes
│   └── runtime.py
├── mandate/                 # Claude mandate parser
│   ├── parser.py            # NL → MandateSchema
│   └── validator.py         # schema rule checks
├── indexer/                 # event listener → DB
│   ├── listener.py
│   └── handlers.py
├── jobs/                    # APScheduler cron
│   ├── harvest.py           # YieldHarvester.harvest() per agent
│   ├── distribute.py        # monthly DividendDistributor.distribute()
│   ├── rebalance.py         # per-agent rebalance trigger
│   └── advance_phase.py     # Incubation → PublicLaunch on day 30
├── chain/                   # web3.py wrappers
│   ├── client.py            # Web3 instance, gas estimation
│   ├── contracts/           # one file per contract — typed wrapper class
│   │   ├── agent_vault.py
│   │   ├── helm_registry.py
│   │   ├── pyth_adapter.py
│   │   └── ...
│   └── abis/                # JSON, sync'd from contracts/out/
└── models/                  # SQLAlchemy DB models
    ├── agent.py
    ├── decision.py
    └── dividend.py
```

## Source-of-truth flow for FE types

```
backend/app/schemas.py            (Pydantic — edit here only)
        │
        │ FastAPI auto-exposes
        ▼
http://localhost:8000/openapi.json
        │
        │ openapi-typescript (in FE)
        ▼
frontend/src/lib/api-types.gen.ts (consumed by FE — do not edit)
```

When you change a Pydantic model, FE re-runs `pnpm gen-types` and gets fresh TS.
See [`docs/frontend/openapi-typegen.md`](../docs/frontend/openapi-typegen.md).

The hand-written `docs/frontend/api-types.ts` shipped initially is reference-only.
Once FE wires `gen-types`, that file is replaced by `api-types.gen.ts`.

## Test

```bash
pytest -v
```

## Lint / format

```bash
ruff check .
black .
```

## Deploy

TBD. For demo, runs on a single VPS or Railway/Fly.io with `uvicorn` behind nginx.
