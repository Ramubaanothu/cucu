from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Polymarket auth
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polygon_private_key: str = ""
    polygon_rpc_url: str = "https://polygon-rpc.com"

    # Claude
    anthropic_api_key: str = ""

    # Trading
    trading_mode: Literal["paper", "live"] = "paper"
    copy_scale: float = 0.10
    max_exposure_per_market: float = 50.0
    max_total_exposure: float = 500.0
    min_bot_score: int = 70
    scan_interval_seconds: int = 3600
    max_target_wallets: int = 10
    poll_interval_seconds: int = 3
    target_wallets: str = ""  # comma-separated override

    # Position management
    take_profit_pct: float = 0.15     # close position at +15% price gain
    stop_loss_pct: float = 0.10       # close position at -10% price drop
    max_hold_hours: int = 48          # force-close positions older than this
    price_update_interval: int = 60   # seconds between price refresh cycles

    # Internal
    agent_http_port: int = 8000
    log_level: str = "INFO"
    db_path: str = "./data/agent.db"

    @property
    def has_trading_credentials(self) -> bool:
        return bool(self.polymarket_api_key and self.polygon_private_key)

    @property
    def has_claude_credentials(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def pinned_wallets(self) -> list[str]:
        if not self.target_wallets:
            return []
        return [w.strip().lower() for w in self.target_wallets.split(",") if w.strip()]
