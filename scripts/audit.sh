#!/usr/bin/env bash
#
# audit.sh — Production health check for Helm (BE ↔ chain ↔ SC).
#
# Usage:
#   bash scripts/audit.sh
#
# Loads contracts/.env (for MANTLE_SEPOLIA_RPC and MANTLESCAN_KEY) if present.
# Requires: cast (foundry), curl, jq.
#
# Categories:
#   1. BE alive + chain reachable
#   2. Chain ↔ BE consistency (agentCount, vault.registry, vault.usdc, indexer gap)
#   3. SC 19 contracts deployed on chain
#   4. SyntheticAsset whitelist for agent vaults
#   5. Pyth feed freshness
#   6. BE critical endpoint reachability
#
# Exit code: 0 if all PASS or only WARN, 1 if any FAIL.

set -u

# ── color helpers ────────────────────────────────────────────────
G='\033[0;32m'; R='\033[0;31m'; Y='\033[0;33m'; B='\033[1;34m'; N='\033[0m'

PASS=0; WARN=0; FAIL=0

pass() { printf "  ${G}✓${N} %s\n" "$1"; PASS=$((PASS + 1)); }
warn() { printf "  ${Y}!${N} %s\n" "$1"; WARN=$((WARN + 1)); }
fail() { printf "  ${R}✗${N} %s\n" "$1"; FAIL=$((FAIL + 1)); }
hdr()  { printf "\n${B}── %s ──${N}\n" "$1"; }

# ── env / paths ──────────────────────────────────────────────────
BE_URL="${BE_URL:-https://web-production-acacf1.up.railway.app}"

CONTRACTS_ENV="$HOME/Desktop/yyw/contracts/.env"
if [[ -f "$CONTRACTS_ENV" ]]; then
    set -a; . "$CONTRACTS_ENV"; set +a
fi
RPC="${MANTLE_SEPOLIA_RPC:-https://rpc.sepolia.mantle.xyz}"

DEPLOY="$HOME/Desktop/yyw/contracts/deployments/5003.json"
if [[ ! -f "$DEPLOY" ]]; then
    printf "${R}ERROR${N}: $DEPLOY not found\n"; exit 1
fi
for tool in cast curl jq; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf "${R}ERROR${N}: '$tool' not on PATH\n"; exit 1
    fi
done

# Lowercase helper for address comparison (bash 4+ ',,').
lc() { echo "$1" | tr '[:upper:]' '[:lower:]'; }

echo "════════════════════════════════════════════════════════════"
echo "Helm Production Health Audit"
echo "BE:    $BE_URL"
echo "Chain: Mantle Sepolia (5003)  RPC: $RPC"
echo "════════════════════════════════════════════════════════════"

# ── 1. BE alive + chain reachable ────────────────────────────────
hdr "1. BE + chain reachability"

health=$(curl -s --max-time 10 "$BE_URL/health" || echo "")
if [[ -z "$health" ]]; then
    fail "GET /health: no response"
elif echo "$health" | jq -e '.ok and .db and .chain' >/dev/null 2>&1; then
    pass "GET /health: ok=true, db=true, chain=true"
else
    fail "GET /health unhealthy: $(echo "$health" | jq -c .)"
fi

# ── 2. Chain ↔ BE consistency ────────────────────────────────────
hdr "2. Chain ↔ BE consistency"

REGISTRY_ADDR=$(jq -r '.registry' "$DEPLOY")
USDC_ADDR=$(jq -r '.usdc' "$DEPLOY")

be_total=$(curl -s "$BE_URL/agents" | jq -r '.total // empty' 2>/dev/null)
chain_count=$(cast call "$REGISTRY_ADDR" "agentCount()(uint256)" --rpc-url "$RPC" 2>/dev/null || echo "")

if [[ -z "$be_total" || -z "$chain_count" ]]; then
    fail "agentCount fetch failed (BE='$be_total' chain='$chain_count')"
elif [[ "$be_total" == "$chain_count" ]]; then
    pass "agentCount: BE=$be_total ↔ chain=$chain_count"
else
    fail "agentCount mismatch: BE=$be_total ≠ chain=$chain_count"
fi

# Per-agent: vault.registry(), vault.usdc(), vault code
agent_ids=$(curl -s "$BE_URL/agents" | jq -r '.items[].agentId')
expected_reg_lc=$(lc "$REGISTRY_ADDR")
expected_usdc_lc=$(lc "$USDC_ADDR")

for aid in $agent_ids; do
    vault_addr=$(curl -s "$BE_URL/agents/$aid" | jq -r '.vaultAddress // empty')
    if [[ -z "$vault_addr" ]]; then
        fail "agent $aid: no vaultAddress in /agents/$aid response"
        continue
    fi

    code=$(cast code "$vault_addr" --rpc-url "$RPC" 2>/dev/null || echo "0x")
    if [[ "${#code}" -le 3 ]]; then
        fail "agent $aid: vault $vault_addr has no on-chain code"
        continue
    fi

    actual_reg=$(cast call "$vault_addr" "registry()(address)" --rpc-url "$RPC" 2>/dev/null || echo "")
    if [[ -z "$actual_reg" ]]; then
        fail "agent $aid: vault.registry() call failed"
    elif [[ "$(lc "$actual_reg")" == "$expected_reg_lc" ]]; then
        pass "agent $aid: vault.registry() matches"
    else
        fail "agent $aid: vault.registry()=$actual_reg ≠ env=$REGISTRY_ADDR"
    fi

    actual_usdc=$(cast call "$vault_addr" "usdc()(address)" --rpc-url "$RPC" 2>/dev/null || echo "")
    if [[ -z "$actual_usdc" ]]; then
        fail "agent $aid: vault.usdc() call failed"
    elif [[ "$(lc "$actual_usdc")" == "$expected_usdc_lc" ]]; then
        pass "agent $aid: vault.usdc() matches"
    else
        fail "agent $aid: vault.usdc()=$actual_usdc ≠ env=$USDC_ADDR"
    fi
done

# Indexer gap
indexer_state=$(curl -s "$BE_URL/admin/debug/indexer-state" 2>/dev/null || echo "")
if [[ -n "$indexer_state" ]] && gap=$(echo "$indexer_state" | jq -r '.gap // empty' 2>/dev/null) && [[ -n "$gap" ]]; then
    if [[ "$gap" -lt 50 ]] 2>/dev/null; then
        pass "indexer gap: $gap blocks"
    elif [[ "$gap" -lt 500 ]] 2>/dev/null; then
        warn "indexer gap: $gap blocks (still syncing)"
    else
        fail "indexer gap: $gap blocks (stuck?)"
    fi
else
    warn "/admin/debug/indexer-state: no usable response"
fi

# ── 3. SC 19 contracts deployed ──────────────────────────────────
hdr "3. SC 19 contracts deployed"
for key in $(jq -r 'keys[]' "$DEPLOY"); do
    addr=$(jq -r --arg k "$key" '.[$k]' "$DEPLOY")
    code=$(cast code "$addr" --rpc-url "$RPC" 2>/dev/null || echo "0x")
    if [[ "${#code}" -gt 100 ]]; then
        pass "$key: $addr (code ${#code} chars)"
    else
        fail "$key: $addr has no code (${#code} chars)"
    fi
done

# ── 4. SyntheticAsset whitelist for agent vaults ─────────────────
hdr "4. SyntheticAsset whitelist"
for aid in $agent_ids; do
    vault_addr=$(curl -s "$BE_URL/agents/$aid" | jq -r '.vaultAddress // empty')
    [[ -z "$vault_addr" ]] && continue
    any_whitelisted=0
    for synth_key in sNVDA sSPY sAAPL sTSLA sMSFT; do
        synth_addr=$(jq -r --arg k "$synth_key" '.[$k]' "$DEPLOY")
        is_reg=$(cast call "$synth_addr" "registeredVaults(address)(bool)" "$vault_addr" --rpc-url "$RPC" 2>/dev/null || echo "")
        if [[ "$is_reg" == "true" ]]; then
            pass "agent $aid $synth_key: whitelisted"
            any_whitelisted=1
        fi
    done
    if [[ $any_whitelisted -eq 0 ]]; then
        warn "agent $aid: no synthetic whitelisted (OK if mandate uses only USDY/mETH)"
    fi
done

# ── 5. Pyth feed freshness ───────────────────────────────────────
hdr "5. Pyth feed freshness"
synth_prices=$(curl -s --max-time 10 "$BE_URL/admin/debug/synthetic-prices" 2>/dev/null || echo "")
if [[ -z "$synth_prices" ]] || ! echo "$synth_prices" | jq -e . >/dev/null 2>&1; then
    warn "/admin/debug/synthetic-prices unreachable — skipping"
else
    for sym in sNVDA sSPY sAAPL sTSLA sMSFT; do
        stale=$(echo "$synth_prices" | jq -r ".$sym.stale // empty")
        err=$(echo "$synth_prices" | jq -r ".$sym.error // empty")
        if [[ "$stale" == "false" ]]; then
            pass "$sym: fresh"
        elif [[ -n "$err" && "$err" != "null" ]]; then
            warn "$sym: error '$err' (often market closed)"
        else
            warn "$sym: stale (may need pythAdapter.updatePriceFeeds)"
        fi
    done
fi

# ── 6. BE critical endpoints ─────────────────────────────────────
hdr "6. BE critical endpoints"
for ep in \
    "/system/info" \
    "/agents" \
    "/admin/debug/treasury" \
    "/admin/debug/adapters" \
    "/admin/debug/indexer-state"; do
    status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BE_URL$ep" || echo "000")
    if [[ "$status" == "200" ]]; then
        pass "GET $ep: 200"
    else
        fail "GET $ep: HTTP $status"
    fi
done

# ── Summary ──────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
printf "Summary:  ${G}%d PASS${N}  ${Y}%d WARN${N}  ${R}%d FAIL${N}\n" "$PASS" "$WARN" "$FAIL"
echo "════════════════════════════════════════════════════════════"

[[ $FAIL -gt 0 ]] && exit 1
exit 0
