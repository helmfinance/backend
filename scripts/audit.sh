#!/usr/bin/env bash
#
# audit.sh v2 — Comprehensive Helm service health check.
#
# Usage:
#   bash scripts/audit.sh
#
# Loads contracts/.env (for MANTLE_SEPOLIA_RPC) if present.
# Requires: cast (foundry), curl, jq.
#
# Categories:
#   1.  BE + RPC reachability
#   2.  19 contracts deployed
#   3.  System contract wiring matrix
#   4.  Per-agent vault wiring
#   5.  Per-agent redemption tier configuration (mandate ↔ chain)
#   6.  Per-agent FounderVault state
#   7.  SyntheticAsset whitelist for agent vaults
#   8.  PythPriceAdapter feed configuration
#   9.  Adapter health (mETH / USDY)
#   10. PlatformTreasury fee rates
#   11. YieldHarvester sources per agent
#   12. BE schema integrity (FE-critical fields)
#   13. Pyth feed freshness
#   14. Indexer gap
#   15. BE endpoint integrity
#
# Read-only. Idempotent. Exit code: 0 if no FAIL, 1 otherwise.

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

# ── helpers ──────────────────────────────────────────────────────
lc() { echo "$1" | tr '[:upper:]' '[:lower:]'; }

# Read a (chain) view function and echo the result, or echo "" on error.
cast_call() {
    cast call "$1" "$2" "${@:3}" --rpc-url "$RPC" 2>/dev/null
}

# Compare two addresses case-insensitively; pass/fail labelled by $1.
expect_addr() {
    local label="$1" actual="$2" expected="$3"
    if [[ -z "$actual" ]]; then
        fail "$label: call failed or empty"
    elif [[ "$(lc "$actual")" == "$(lc "$expected")" ]]; then
        pass "$label"
    else
        fail "$label: got $actual ≠ expected $expected"
    fi
}

# ── load deployment addresses ────────────────────────────────────
REGISTRY=$(jq -r '.registry' "$DEPLOY")
USDC=$(jq -r '.usdc' "$DEPLOY")
AGENT_NFT=$(jq -r '.agentNFT' "$DEPLOY")
TIME_PROVIDER=$(jq -r '.timeProvider' "$DEPLOY")
REDEMPTION_QUEUE=$(jq -r '.redemptionQueue' "$DEPLOY")
TREASURY=$(jq -r '.treasury' "$DEPLOY")
HARVESTER=$(jq -r '.harvester' "$DEPLOY")
DISTRIBUTOR=$(jq -r '.distributor' "$DEPLOY")
PYTH_ADAPTER=$(jq -r '.pythAdapter' "$DEPLOY")
METH_ADAPTER=$(jq -r '.mEthAdapter' "$DEPLOY")
USDY_ADAPTER=$(jq -r '.usdyAdapter' "$DEPLOY")
AGENT_TOKEN_IMPL=$(jq -r '.agentTokenImpl' "$DEPLOY")
AGENT_VAULT_IMPL=$(jq -r '.agentVaultImpl' "$DEPLOY")
FOUNDER_VAULT_IMPL=$(jq -r '.founderVaultImpl' "$DEPLOY")

# Lookup table: symbol → address (for synthetic asset whitelist check).
declare -A SYNTH_ADDR
SYNTH_ADDR[sNVDA]=$(jq -r '.sNVDA' "$DEPLOY")
SYNTH_ADDR[sSPY]=$(jq -r '.sSPY' "$DEPLOY")
SYNTH_ADDR[sAAPL]=$(jq -r '.sAAPL' "$DEPLOY")
SYNTH_ADDR[sTSLA]=$(jq -r '.sTSLA' "$DEPLOY")
SYNTH_ADDR[sMSFT]=$(jq -r '.sMSFT' "$DEPLOY")

# Fetch agent IDs from BE early; used by per-agent categories.
AGENT_IDS=$(curl -s "$BE_URL/agents" | jq -r '.items[].agentId // empty')

echo "════════════════════════════════════════════════════════════"
echo "Helm Production Health Audit v2"
echo "BE:     $BE_URL"
echo "Chain:  Mantle Sepolia (5003)  RPC: $RPC"
echo "Agents: $(echo $AGENT_IDS | tr '\n' ' ')"
echo "════════════════════════════════════════════════════════════"

# ── 1. BE + RPC reachability ─────────────────────────────────────
hdr "1. BE + RPC reachability"

health=$(curl -s --max-time 10 "$BE_URL/health" || echo "")
if [[ -z "$health" ]]; then
    fail "GET /health: no response"
elif echo "$health" | jq -e '.ok and .db and .chain' >/dev/null 2>&1; then
    pass "GET /health: ok=true, db=true, chain=true"
else
    fail "GET /health unhealthy: $(echo "$health" | jq -c .)"
fi

block=$(cast block-number --rpc-url "$RPC" 2>/dev/null || echo "")
if [[ -n "$block" && "$block" -gt 0 ]] 2>/dev/null; then
    pass "RPC eth_blockNumber: $block"
else
    fail "RPC unreachable or returned non-numeric: '$block'"
fi

# ── 2. 19 contracts deployed ─────────────────────────────────────
hdr "2. SC 19 contracts deployed"
for key in $(jq -r 'keys[]' "$DEPLOY"); do
    addr=$(jq -r --arg k "$key" '.[$k]' "$DEPLOY")
    code=$(cast code "$addr" --rpc-url "$RPC" 2>/dev/null || echo "0x")
    if [[ "${#code}" -gt 100 ]]; then
        pass "$key: $addr (code ${#code} chars)"
    else
        fail "$key: $addr has no code (${#code} chars)"
    fi
done

# ── 3. System wiring matrix ──────────────────────────────────────
hdr "3. System contract wiring matrix"

# HelmRegistry → USDC / agentNFT / timeProvider / redemptionQueue / treasury / pythAdapter / distributor / executor / impls
expect_addr "HelmRegistry.usdc"             "$(cast_call $REGISTRY 'usdc()(address)')"             "$USDC"
expect_addr "HelmRegistry.agentNFT"         "$(cast_call $REGISTRY 'agentNFT()(address)')"         "$AGENT_NFT"
expect_addr "HelmRegistry.timeProvider"     "$(cast_call $REGISTRY 'timeProvider()(address)')"     "$TIME_PROVIDER"
expect_addr "HelmRegistry.redemptionQueue"  "$(cast_call $REGISTRY 'redemptionQueue()(address)')"  "$REDEMPTION_QUEUE"
expect_addr "HelmRegistry.treasury"         "$(cast_call $REGISTRY 'treasury()(address)')"         "$TREASURY"
expect_addr "HelmRegistry.pythAdapter"      "$(cast_call $REGISTRY 'pythAdapter()(address)')"      "$PYTH_ADAPTER"
expect_addr "HelmRegistry.distributor"      "$(cast_call $REGISTRY 'distributor()(address)')"      "$DISTRIBUTOR"
expect_addr "HelmRegistry.agentTokenImpl"   "$(cast_call $REGISTRY 'agentTokenImpl()(address)')"   "$AGENT_TOKEN_IMPL"
expect_addr "HelmRegistry.agentVaultImpl"   "$(cast_call $REGISTRY 'agentVaultImpl()(address)')"   "$AGENT_VAULT_IMPL"
expect_addr "HelmRegistry.founderVaultImpl" "$(cast_call $REGISTRY 'founderVaultImpl()(address)')" "$FOUNDER_VAULT_IMPL"

# RedemptionQueue → registry / timeProvider
expect_addr "RedemptionQueue.registry"     "$(cast_call $REDEMPTION_QUEUE 'registry()(address)')"     "$REGISTRY"
expect_addr "RedemptionQueue.timeProvider" "$(cast_call $REDEMPTION_QUEUE 'timeProvider()(address)')" "$TIME_PROVIDER"

# AgentNFT → registry / timeProvider
expect_addr "AgentNFT.registry"     "$(cast_call $AGENT_NFT 'registry()(address)')"     "$REGISTRY"
expect_addr "AgentNFT.timeProvider" "$(cast_call $AGENT_NFT 'timeProvider()(address)')" "$TIME_PROVIDER"

# PlatformTreasury → usdc
expect_addr "PlatformTreasury.usdc" "$(cast_call $TREASURY 'usdc()(address)')" "$USDC"

# YieldHarvester → registry / usdc / timeProvider
expect_addr "YieldHarvester.registry"     "$(cast_call $HARVESTER 'registry()(address)')"     "$REGISTRY"
expect_addr "YieldHarvester.usdc"         "$(cast_call $HARVESTER 'usdc()(address)')"         "$USDC"
expect_addr "YieldHarvester.timeProvider" "$(cast_call $HARVESTER 'timeProvider()(address)')" "$TIME_PROVIDER"

# DividendDistributor → harvester / registry / usdc / timeProvider
expect_addr "DividendDistributor.harvester"   "$(cast_call $DISTRIBUTOR 'harvester()(address)')"   "$HARVESTER"
expect_addr "DividendDistributor.registry"    "$(cast_call $DISTRIBUTOR 'registry()(address)')"    "$REGISTRY"
expect_addr "DividendDistributor.usdc"        "$(cast_call $DISTRIBUTOR 'usdc()(address)')"        "$USDC"
expect_addr "DividendDistributor.timeProvider" "$(cast_call $DISTRIBUTOR 'timeProvider()(address)')" "$TIME_PROVIDER"

# ── 4. Per-agent vault wiring ────────────────────────────────────
hdr "4. Per-agent vault wiring"
for aid in $AGENT_IDS; do
    vault=$(curl -s "$BE_URL/agents/$aid" | jq -r '.vaultAddress // empty')
    if [[ -z "$vault" ]]; then
        fail "agent $aid: no vaultAddress in /agents/$aid"
        continue
    fi

    expect_addr "agent $aid: vault.registry"        "$(cast_call $vault 'registry()(address)')"        "$REGISTRY"
    expect_addr "agent $aid: vault.usdc"            "$(cast_call $vault 'usdc()(address)')"            "$USDC"
    expect_addr "agent $aid: vault.treasury"        "$(cast_call $vault 'treasury()(address)')"        "$TREASURY"
    expect_addr "agent $aid: vault.redemptionQueue" "$(cast_call $vault 'redemptionQueue()(address)')" "$REDEMPTION_QUEUE"
    expect_addr "agent $aid: vault.pythAdapter"     "$(cast_call $vault 'pythAdapter()(address)')"     "$PYTH_ADAPTER"
    expect_addr "agent $aid: vault.yieldHarvester"  "$(cast_call $vault 'yieldHarvester()(address)')"  "$HARVESTER"
    expect_addr "agent $aid: vault.timeProvider"    "$(cast_call $vault 'timeProvider()(address)')"    "$TIME_PROVIDER"

    # agentToken + founderVault are EIP-1167 minimal proxy clones (~92 chars of code).
    for fld in agentToken founderVault; do
        addr=$(cast_call $vault "${fld}()(address)")
        if [[ -z "$addr" || "$addr" == "0x0000000000000000000000000000000000000000" ]]; then
            fail "agent $aid: vault.${fld} zero/empty"
        else
            code=$(cast code "$addr" --rpc-url "$RPC" 2>/dev/null || echo "0x")
            if [[ "${#code}" -gt 50 ]]; then
                pass "agent $aid: vault.${fld} deployed ($addr)"
            else
                fail "agent $aid: vault.${fld}=$addr has no code"
            fi
        fi
    done
done

# ── 5. Per-agent redemption tier configuration ───────────────────
hdr "5. Per-agent redemption tier configuration"
# Map mandate string → chain enum index
declare -A TIER_INDEX
TIER_INDEX[instant]=0
TIER_INDEX["30d"]=1
TIER_INDEX["60d"]=2
TIER_INDEX["90d"]=3
TIER_NAMES=("instant" "30d" "60d" "90d")

for aid in $AGENT_IDS; do
    mandate_lockups=$(curl -s "$BE_URL/agents/$aid" | jq -r '.allowedLockups[]?' 2>/dev/null)
    if [[ -z "$mandate_lockups" ]]; then
        warn "agent $aid: BE returned no allowedLockups (mandate missing?)"
        continue
    fi

    # Build expected bool[4] from mandate.
    declare -A want
    for i in 0 1 2 3; do want[$i]=false; done
    for lk in $mandate_lockups; do
        idx="${TIER_INDEX[$lk]:-}"
        [[ -n "$idx" ]] && want[$idx]=true
    done

    # Read chain state and compare.
    for i in 0 1 2 3; do
        got=$(cast_call "$REDEMPTION_QUEUE" "tierAllowed(uint256,uint8)(bool)" "$aid" "$i")
        if [[ "$got" == "${want[$i]}" ]]; then
            pass "agent $aid tier ${TIER_NAMES[$i]}: chain=$got matches mandate"
        else
            fail "agent $aid tier ${TIER_NAMES[$i]}: chain=$got but mandate wants ${want[$i]}"
        fi
    done
    unset want
done

# ── 6. Per-agent FounderVault state ──────────────────────────────
hdr "6. Per-agent FounderVault state"
for aid in $AGENT_IDS; do
    detail=$(curl -s "$BE_URL/agents/$aid")
    vault=$(echo "$detail" | jq -r '.vaultAddress // empty')
    founder_be=$(echo "$detail" | jq -r '.founderAddress // empty')
    [[ -z "$vault" ]] && continue

    fv=$(cast_call "$vault" 'founderVault()(address)')
    [[ -z "$fv" || "$fv" == "0x0000000000000000000000000000000000000000" ]] && { fail "agent $aid: vault.founderVault empty"; continue; }

    fv_founder=$(cast_call "$fv" 'founder()(address)')
    if [[ -z "$fv_founder" || "$fv_founder" == "0x0000000000000000000000000000000000000000" ]]; then
        fail "agent $aid: founderVault.founder = 0"
    elif [[ -n "$founder_be" && "$(lc $fv_founder)" != "$(lc $founder_be)" ]]; then
        fail "agent $aid: founderVault.founder=$fv_founder ≠ BE.founderAddress=$founder_be"
    else
        pass "agent $aid: founderVault.founder=$fv_founder"
    fi

    expect_addr "agent $aid: founderVault.vault" "$(cast_call $fv 'vault()(address)')" "$vault"

    lockup_end=$(cast_call "$fv" 'lockupEndsAt()(uint256)' | awk '{print $1}')
    if [[ -n "$lockup_end" && "$lockup_end" -gt 0 ]] 2>/dev/null; then
        pass "agent $aid: founderVault.lockupEndsAt=$lockup_end"
    else
        fail "agent $aid: founderVault.lockupEndsAt invalid ($lockup_end)"
    fi

    fbps=$(cast_call "$fv" 'founderShareBps()(uint256)' | awk '{print $1}')
    if [[ -n "$fbps" && "$fbps" -ge 500 && "$fbps" -le 3000 ]] 2>/dev/null; then
        pass "agent $aid: founderShareBps=$fbps (range 500-3000)"
    else
        fail "agent $aid: founderShareBps=$fbps out of [500,3000]"
    fi

    sbps=$(cast_call "$fv" 'subordinationThresholdBps()(uint256)' | awk '{print $1}')
    if [[ -n "$sbps" && "$sbps" -gt 0 ]] 2>/dev/null; then
        pass "agent $aid: subordinationThresholdBps=$sbps"
    else
        warn "agent $aid: subordinationThresholdBps=$sbps (0 → no protection)"
    fi
done

# ── 7. SyntheticAsset whitelist for agent vaults ─────────────────
hdr "7. SyntheticAsset whitelist"
for aid in $AGENT_IDS; do
    detail=$(curl -s "$BE_URL/agents/$aid")
    vault=$(echo "$detail" | jq -r '.vaultAddress // empty')
    [[ -z "$vault" ]] && continue

    # Pull mandate.targetUniverse so we only check synth assets the agent actually uses.
    universe=$(echo "$detail" | jq -r '.mandate.targetUniverse[]?' 2>/dev/null)
    any_synth=0
    for sym in $universe; do
        addr="${SYNTH_ADDR[$sym]:-}"
        [[ -z "$addr" ]] && continue
        any_synth=1
        is_reg=$(cast_call "$addr" 'registeredVaults(address)(bool)' "$vault")
        if [[ "$is_reg" == "true" ]]; then
            pass "agent $aid $sym: whitelisted"
        else
            fail "agent $aid $sym: NOT whitelisted (mandate uses it but registeredVaults=false)"
        fi
    done
    [[ $any_synth -eq 0 ]] && pass "agent $aid: mandate uses no synth equities (skip)"
done

# ── 8. PythPriceAdapter feed configuration ───────────────────────
hdr "8. PythPriceAdapter feed configuration"
# Pyth feed IDs are env-supplied; pull from BE /system/info if available.
sysinfo=$(curl -s "$BE_URL/system/info" 2>/dev/null || echo "")
if [[ -z "$sysinfo" ]] || ! echo "$sysinfo" | jq -e . >/dev/null 2>&1; then
    warn "/system/info unreachable — cannot enumerate Pyth feeds"
else
    feed_ids=$(echo "$sysinfo" | jq -r '.pythFeedIds // {} | to_entries[] | "\(.key)=\(.value)"' 2>/dev/null)
    if [[ -z "$feed_ids" ]]; then
        warn "/system/info has no pythFeedIds field"
    else
        for kv in $feed_ids; do
            sym="${kv%%=*}"
            fid="${kv##*=}"
            [[ -z "$fid" || "$fid" == "null" ]] && continue
            registered=$(cast_call "$PYTH_ADAPTER" 'feedRegistered(bytes32)(bool)' "$fid" 2>/dev/null)
            stale=$(cast_call "$PYTH_ADAPTER" 'maxStaleness(bytes32)(uint64)' "$fid" 2>/dev/null | awk '{print $1}')
            if [[ "$registered" == "true" && -n "$stale" && "$stale" -gt 0 ]] 2>/dev/null; then
                pass "Pyth $sym: registered, maxStaleness=${stale}s"
            elif [[ "$sym" =~ ^s[A-Z]+$ ]]; then
                # Synthetic equity feed — required.
                fail "Pyth $sym (feedId=$fid): registered=$registered, maxStaleness=$stale"
            else
                # Macro feed (ETH/USD, USDC/USD) — only used if adapters query it.
                warn "Pyth $sym: not registered on adapter (OK if unused by adapters)"
            fi
        done
    fi
fi

# ── 9. Adapter health (mETH / USDY) ──────────────────────────────
hdr "9. Adapter health"
for pair in "mEth:$METH_ADAPTER" "USDY:$USDY_ADAPTER"; do
    name="${pair%%:*}"
    addr="${pair##*:}"
    rate=$(cast_call "$addr" 'exchangeRate()(uint256)' | awk '{print $1}')
    if [[ -n "$rate" && "$rate" -gt 0 ]] 2>/dev/null; then
        pass "$name adapter exchangeRate=$rate (>0)"
    else
        fail "$name adapter exchangeRate=$rate (invalid)"
    fi
done

# ── 10. PlatformTreasury fee rates ───────────────────────────────
hdr "10. PlatformTreasury fee rates"
fees=$(cast_call "$TREASURY" 'feeRates()(uint256,uint256,uint256)')
if [[ -z "$fees" ]]; then
    fail "treasury.feeRates() call failed"
else
    # cast prints each tuple element on its own line — flatten with tr first.
    read mint_bps redeem_bps rebal_bps <<<"$(echo "$fees" | tr '\n' ' ')"
    if [[ "$mint_bps" -gt 0 && "$mint_bps" -le 1000 ]] 2>/dev/null; then
        pass "feeRates.mint=$mint_bps bps (≤ MAX_FEE_BPS 1000)"
    else
        fail "feeRates.mint=$mint_bps bps (out of (0,1000])"
    fi
    if [[ "$redeem_bps" -gt 0 && "$redeem_bps" -le 1000 ]] 2>/dev/null; then
        pass "feeRates.redeem=$redeem_bps bps"
    else
        fail "feeRates.redeem=$redeem_bps bps (out of (0,1000])"
    fi
    if [[ "$rebal_bps" -gt 0 && "$rebal_bps" -le 1000 ]] 2>/dev/null; then
        pass "feeRates.rebalance=$rebal_bps bps"
    else
        fail "feeRates.rebalance=$rebal_bps bps (out of (0,1000])"
    fi
fi

# ── 11. YieldHarvester sources per agent ─────────────────────────
hdr "11. YieldHarvester sources per agent"
for aid in $AGENT_IDS; do
    # mETH source check
    meth_idx=$(cast_call "$HARVESTER" '_sourceIndex(uint256,address)(uint256)' "$aid" "$METH_ADAPTER" 2>/dev/null | awk '{print $1}')
    usdy_idx=$(cast_call "$HARVESTER" '_sourceIndex(uint256,address)(uint256)' "$aid" "$USDY_ADAPTER" 2>/dev/null | awk '{print $1}')
    # _sourceIndex is internal in solidity — may not be callable. Fall back to a soft check via vault assets.
    if [[ -z "$meth_idx" && -z "$usdy_idx" ]]; then
        warn "agent $aid: harvester source map not introspectable (private getter)"
    else
        if [[ "$meth_idx" -gt 0 ]] 2>/dev/null; then pass "agent $aid: harvester has mETH source"
        else warn "agent $aid: harvester has no mETH source"; fi
        if [[ "$usdy_idx" -gt 0 ]] 2>/dev/null; then pass "agent $aid: harvester has USDY source"
        else warn "agent $aid: harvester has no USDY source"; fi
    fi
done

# ── 12. BE schema integrity (FE-critical fields) ─────────────────
hdr "12. BE schema integrity (FE-critical)"
# /system/info top-level fields
if [[ -n "$sysinfo" ]] && echo "$sysinfo" | jq -e '.chainId and .contracts and .syntheticAssets' >/dev/null 2>&1; then
    pass "/system/info has chainId + contracts + syntheticAssets"
else
    fail "/system/info missing required fields"
fi

# Per-agent: check AgentDetail fields the FE actually reads
REQ_FIELDS=(agentId name ticker vaultAddress tokenAddress founderAddress \
            navUsdc navPerShareUsdc phase totalShares holderCount reputation \
            mandate positions allowedLockups)
for aid in $AGENT_IDS; do
    detail=$(curl -s "$BE_URL/agents/$aid")
    missing=()
    for f in "${REQ_FIELDS[@]}"; do
        val=$(echo "$detail" | jq -r ".$f // empty")
        [[ -z "$val" || "$val" == "null" ]] && missing+=("$f")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        pass "agent $aid: all ${#REQ_FIELDS[@]} required FE fields present"
    else
        fail "agent $aid: missing/null FE fields: ${missing[*]}"
    fi
done

# ── 13. Pyth feed freshness ──────────────────────────────────────
hdr "13. Pyth feed freshness"
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
            warn "$sym: stale (run pythAdapter.updatePriceFeeds)"
        fi
    done
fi

# ── 14. Indexer gap ──────────────────────────────────────────────
hdr "14. Indexer gap"
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

# ── 15. BE endpoint integrity ────────────────────────────────────
hdr "15. BE endpoint integrity"
# First agent for per-agent endpoints
first_agent=$(echo "$AGENT_IDS" | head -n1)
first_founder=""
[[ -n "$first_agent" ]] && first_founder=$(curl -s "$BE_URL/agents/$first_agent" | jq -r '.founderAddress // empty')

declare -a ENDPOINTS=(
    "/health"
    "/system/info"
    "/agents"
    "/admin/debug/treasury"
    "/admin/debug/adapters"
    "/admin/debug/synthetic-prices"
    "/admin/debug/indexer-state"
)
[[ -n "$first_agent" ]] && ENDPOINTS+=(
    "/agents/$first_agent"
    "/agents/$first_agent/nav-history?period=24h"
    "/agents/$first_agent/decisions"
    "/admin/debug/agents/$first_agent/founder-vault"
    "/admin/debug/agents/$first_agent/redemption-queue"
    "/admin/debug/agents/$first_agent/compare"
)
[[ -n "$first_founder" ]] && ENDPOINTS+=("/portfolio/$first_founder")

for ep in "${ENDPOINTS[@]}"; do
    resp=$(curl -s -w "\n%{http_code}" --max-time 15 "$BE_URL$ep" 2>/dev/null)
    status=$(echo "$resp" | tail -n1)
    body=$(echo "$resp" | sed '$d')
    if [[ "$status" == "200" ]]; then
        if echo "$body" | jq -e . >/dev/null 2>&1; then
            pass "GET $ep: 200 (JSON ok)"
        else
            fail "GET $ep: 200 but body not valid JSON"
        fi
    elif [[ "$status" == "501" ]]; then
        # 501 = Not Implemented (BE stub). FE handles via empty-state fallback.
        warn "GET $ep: HTTP 501 (BE schema-only stub)"
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
