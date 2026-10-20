"""Chargement de la configuration TOML en dataclasses typées."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Fees:
    maker: float = 0.0025
    taker: float = 0.005
    slippage: float = 0.0005


@dataclass
class StrategyCfg:
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    rsi_period: int = 14
    rsi_min: float = 50
    rsi_max: float = 70
    atr_period: int = 14
    stop_atr: float = 1.5
    reward_risk: float = 2.0
    min_edge_over_fees: float = 3.0
    entry_offset_atr: float = 0.1
    entry_ttl_bars: int = 2
    max_hold_bars: int = 32


@dataclass
class RiskCfg:
    risk_per_trade: float = 0.005
    max_position_pct: float = 0.25
    max_open_positions: int = 2
    max_trades_per_day: int = 6
    max_daily_loss: float = 0.02
    max_drawdown: float = 0.10
    min_order_quote: float = 10.0
    kill_switch_file: str = "data/STOP"
    blackout: list[tuple[datetime, datetime]] = field(default_factory=list)


@dataclass
class Config:
    mode: str = "paper"
    products: list[str] = field(default_factory=lambda: ["BTC-EUR"])
    portfolio_id: str = ""
    quote_currency: str = "EUR"
    signal_tf: str = "FIFTEEN_MINUTE"
    trend_tf: str = "ONE_HOUR"
    fees: Fees = field(default_factory=Fees)
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    paper_balance: float = 1000.0
    journal_path: str = "data/journal.sqlite"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load(path: str | Path = "config.toml") -> Config:
    raw = tomllib.loads(Path(path).read_text())
    risk = dict(raw.get("risk", {}))
    risk["blackout"] = [(_dt(a), _dt(b)) for a, b in risk.get("blackout", [])]
    tf = raw.get("timeframes", {})
    return Config(
        mode=raw.get("mode", "paper"),
        products=raw.get("products", ["BTC-EUR"]),
        portfolio_id=raw.get("portfolio_id", ""),
        quote_currency=raw.get("quote_currency", "EUR"),
        signal_tf=tf.get("signal", "FIFTEEN_MINUTE"),
        trend_tf=tf.get("trend", "ONE_HOUR"),
        fees=Fees(**raw.get("fees", {})),
        strategy=StrategyCfg(**raw.get("strategy", {})),
        risk=RiskCfg(**risk),
        paper_balance=raw.get("paper", {}).get("starting_balance", 1000.0),
        journal_path=raw.get("journal", {}).get("path", "data/journal.sqlite"),
    )
