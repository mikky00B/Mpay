"""Application configuration via environment variables (pydantic-settings).

Everything is overridable by env; defaults make local dev + tests work with
zero setup (SQLite, per [D3]).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Database [D3] ---
    database_url: str = "sqlite:///./mpay.db"

    # --- Chain / watcher [D5][D6] ---
    # Shared hot address that receives all USDC payments (D6).
    receiving_address: str = "0x0000000000000000000000000000000000000000"
    # USDC contract on Ethereum mainnet; tests override with a fake address.
    usdc_contract: str = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    # Confirmations required before an invoice flips CONFIRMING -> CONFIRMED.
    confirmation_threshold: int = 12
    # Shared secret between the watcher and the internal ingest API (D5).
    internal_api_key: str = "dev-internal-key"

    # --- Invoice lifecycle ---
    # Minutes before an AWAITING_PAYMENT invoice expires.
    invoice_ttl_minutes: int = 60

    # --- Chain access (confirmation counter) ---
    # JSON-RPC endpoint for eth_blockNumber; empty = offline mode (tests inject
    # block heights directly instead).
    rpc_url: str = ""

    # --- Background loop intervals (seconds) ---
    processor_poll_seconds: float = 1.0
    sweep_poll_seconds: float = 2.0
    dispatcher_poll_seconds: float = 1.0

    # --- Webhooks [D8] ---
    webhook_max_attempts: int = 5
    webhook_backoff_base_seconds: float = 2.0
    webhook_backoff_cap_seconds: float = 60.0
    webhook_timeout_seconds: float = 10.0

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
