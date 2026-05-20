# Helm Demo Walkthrough (4-min video)

> Pre-recording: `bash scripts/reset_demo.sh` to clean DB and start uvicorn.

## 0:00–0:25 — Hook

- Screen: Helm landing page
- VO: "Tokenized funds are vulnerable to rug pulls. Helm makes them
  structurally impossible."

## 0:25–0:50 — Marketplace

- Screen: `/agents` listing showing seed agents
- VO: "Each agent passes 30-day on-chain incubation before being public-listed."

## 0:50–1:20 — Mandate authoring

- Screen: textarea → `POST /mandate/parse` → JSON response
- VO: "Founder writes natural-language mandate. OpenAI parses to structured
  constraints. Tier-2 schema enforced — LLM cannot deviate."

## 1:20–1:50 — User mints

- Screen: USDC approve → `vault.mint` with Pyth bytes
- VO: "Investor mints shares at NAV. Pyth feeds 5 synthetic equities."

## 1:50–2:30 — Time advance + AI ops

- Screen: `admin/time/advance` 30d → `admin/agents/X/rebalance`
- VO: "30 days fast-forwarded. AI agent rebalances per mandate constraints.
  Decision engine deterministic, narrator note via gpt-4o."

## 2:30–2:55 — Redemption queue

- Screen: agent's `allowedLockups` display
- VO: "Mandate-set lockup. Longer queue → reputation premium → dev incentive."

## 2:55–3:35 — Rug-pull protection

- Screen: founder vault status
- VO: "Dev tries 40% withdrawal — contract reverts. Preventive, not reactive.
  Wind-down only via legitimate signals: `signalWindDown`, mandate breach,
  or reputation slash."

## 3:35–3:55 — NFT lifetime

- Screen: NFT metadata viewer or OpenSea preview
- VO: "ERC-8004 NFT carries lifetime performance: Sharpe, drawdown,
  rebalance count, holders, yield distributed, dev carry."

## 3:55–4:00 — Vision close

- Screen: GitHub link + deployed contracts table
- VO: "REIT-model AI fund layer on Mantle. Open source. Production roadmap:
  Init Capital, Merchant Moe DEX routing, Pendle PT integration."
