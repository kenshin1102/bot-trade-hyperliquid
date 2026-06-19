from __future__ import annotations
from dataclasses import dataclass, field, fields
from pathlib import Path
import yaml
from pydantic_settings import BaseSettings


@dataclass
class DataConfig:
    coins: list = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    candle_intervals: list = field(default_factory=lambda: ["15m", "1h"])
    backfill_days: int = 90


@dataclass
class RegimeConfig:
    no_trade_below: float = 30.0
    small_size_below: float = 50.0
    btc_trend_weight: float = 0.30
    coin_trend_weight: float = 0.25
    volume_weight: float = 0.20
    funding_weight: float = 0.15
    oi_weight: float = 0.10


@dataclass
class StrategyConfig:
    name: str = "Breakout_V1"
    timeframe: str = "15m"
    breakout_lookback_candles: int = 20
    volume_zscore_min: float = 1.5
    oi_change_min_pct: float = 0.02
    funding_max_pct: float = 0.005
    spread_max_bps: float = 20.0


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: float = 0.25
    sl_atr_multiplier: float = 1.5
    tp_rr: float = 2.0
    max_daily_loss_pct: float = 1.0
    max_concurrent_positions: int = 3
    emergency_stop: bool = False


@dataclass
class ExecutionConfig:
    mode: str = "paper"
    account_balance: float = 10000.0
    fee_taker_bps: float = 2.5
    slippage_bps: float = 5.0


@dataclass
class Config:
    data: DataConfig = field(default_factory=DataConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


class Secrets(BaseSettings):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    database_url: str = "postgresql://bot:bot@localhost:5434/hl_strategy"
    redis_url: str = "redis://localhost:6381/0"
    hl_account_address: str = ""
    hl_private_key: str = ""
    live_trading_confirmed: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


def _from_dict(cls, data: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | None = None) -> Config:
    p = Path(path or "config.yaml")
    if not p.exists():
        return Config()
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = Config()
    if "data" in raw:
        cfg.data = _from_dict(DataConfig, raw["data"])
    if "regime" in raw:
        cfg.regime = _from_dict(RegimeConfig, raw["regime"])
    if "strategy" in raw:
        cfg.strategy = _from_dict(StrategyConfig, raw["strategy"])
    if "risk" in raw:
        cfg.risk = _from_dict(RiskConfig, raw["risk"])
    if "execution" in raw:
        cfg.execution = _from_dict(ExecutionConfig, raw["execution"])
    return cfg


def load_secrets() -> Secrets:
    return Secrets()
