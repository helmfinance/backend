"""Helm BE — typed settings loaded from .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Chain ---
    mantle_sepolia_rpc: str = "https://rpc.sepolia.mantle.xyz"
    mantle_rpc: str = "https://rpc.mantle.xyz"
    chain_id: int = 5003

    # --- Cron signer ---
    cron_signer_private_key: str = "0x"

    # --- Anthropic (legacy — kept for fallback) ---
    anthropic_api_key: str = ""

    # --- OpenAI (current LLM provider) ---
    openai_api_key: str = ""
    openai_mandate_model: str = "gpt-4o-mini"
    openai_narrator_model: str = "gpt-4o"

    # --- Database ---
    database_url: str = "sqlite:///./helm.db"

    # --- IPFS pinning ---
    ipfs_pin_provider: str = "web3storage"
    web3_storage_token: str = ""
    pinata_jwt: str = ""

    # --- Pyth ---
    pyth_hermes_url: str = "https://hermes.pyth.network"
    pyth_contract: str = ""
    pyth_feed_nvda: str = ""
    pyth_feed_spy: str = ""
    pyth_feed_aapl: str = ""
    pyth_feed_tsla: str = ""
    pyth_feed_msft: str = ""
    pyth_feed_eth_usd: str = ""
    pyth_feed_usdc_usd: str = ""

    # --- Deployed contract addresses ---
    helm_registry: str = ""
    platform_treasury: str = ""
    redemption_queue: str = ""
    yield_harvester: str = ""
    dividend_distributor: str = ""
    pyth_price_adapter: str = ""
    mantle_meth_adapter: str = ""
    ondo_usdy_adapter: str = ""

    # --- Token addresses ---
    usdc: str = ""
    mantle_meth: str = ""
    ondo_usdy: str = ""

    # ─── New deployed contracts ───────────────────────────────────────────
    agent_nft: str = ""
    time_provider: str = ""
    agent_token_impl: str = ""
    agent_vault_impl: str = ""
    founder_vault_impl: str = ""

    # ─── Synthetic equity addresses ───────────────────────────────────────
    snvda: str = ""
    sspy: str = ""
    saapl: str = ""
    stsla: str = ""
    smsft: str = ""

    # --- Server ---
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000,http://localhost:8080"

    # --- Indexer ---
    # Poll cadence is bounded by per-cycle RPC cost (chunk × N active vaults ×
    # event types). 15s + 200 keeps a single cycle comfortably under interval
    # for the current agent set; if the active count grows past ~30 agents,
    # split vault polling onto its own scheduler job.
    indexer_poll_seconds: int = 15
    # 500 blocks per chunk: 2000 hit Mantle Sepolia RPC limits intermittently
    # (eth_getLogs per contract × 5 contracts × per-vault loop). 500 is the
    # sweet spot — backfill still completes in ~5min (with chunk_size × 5
    # contracts of parallel logs per cycle) and avoids the stuck-scheduler
    # state we saw at 2000.
    indexer_chunk_blocks: int = 500

    # --- Protocol constants (env, not on-chain reads) ---
    mint_fee_bps: int = 50
    redeem_fee_bps: int = 50
    rebalance_fee_bps: int = 5
    carry_bps: int = 1000
    max_leverage: float = 1.0

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
