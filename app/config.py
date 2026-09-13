"""Application configuration via environment variables (pydantic-settings).

Everything is overridable by env; defaults make local dev + tests work with
zero setup (SQLite, per [D3]).
"""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Database [D3] ---
    database_url: str = "sqlite:///./mpay.db"

    # --- Chain / watcher [D5][D6] ---
    # Shared hot address that receives all USDC payments (D6).
    # Normalized to lowercase at load [D17]: .env holds the EIP-55 checksummed
    # form, but chain events are lowercase and SQLite compares case-
    # sensitively — accepting mixed case here silently dropped real payments.
    receiving_address: str = "0x0000000000000000000000000000000000000000"
    # USDC contract on Ethereum mainnet; tests override with a fake address.
    usdc_contract: str = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    # EVM chain id — MUST match the network the RPC/USDC contract belong to.
    # Encoded into EIP-681 payment URIs: without @chainId a URI defaults to
    # mainnet (chain 1), so a Sepolia deployment's QR silently pointed wallets
    # at the wrong network.
    chain_id: int = 1
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

    # --- Reconciliation (build step 7) [D18] ---
    reconciliation_poll_seconds: float = 30.0
    # chain_events pending longer than this => processor loop suspected dead.
    reconciliation_stale_pending_seconds: int = 300
    # Max payments re-verified against the chain per pass (RPC rate limiting).
    reconciliation_crosscheck_batch: int = 25
    # Max webhook re-enqueues per pass (outage can't create a delivery storm).
    reconciliation_rescue_cap: int = 10
    # Reorg window [D19]: payments this many blocks from head are actionable.
    reorg_safety_depth: int = 60

    # --- Webhooks [D8] ---
    webhook_max_attempts: int = 5
    webhook_backoff_base_seconds: float = 2.0
    webhook_backoff_cap_seconds: float = 60.0
    webhook_timeout_seconds: float = 10.0
    # SSRF guard [D22]: block webhook delivery to private/loopback ranges.
    # Enable ONLY for dev rigs whose mock webhook receiver runs on localhost.
    webhook_allow_private_hosts: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}

    @field_validator("receiving_address", "usdc_contract")
    @classmethod
    def _canonicalize_address(cls, v: str) -> str:
        """Lowercase hex identifiers once, at the single source of truth [D17]."""
        return v.strip().lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
