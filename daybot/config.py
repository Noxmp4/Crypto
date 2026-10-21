"""Chargement de la configuration TOML en dataclasses typées."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.9/3.10 (ex. /usr/bin/python3 de macOS)
    import tomli as tomllib


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
class TrendCfg:
    """Poche tactique : suivi de tendance journalier (paramètres de manuel, non optimisés)."""
    enabled: bool = True
    products: list[str] = field(default_factory=lambda: ["BTC-EUR", "ETH-EUR", "SOL-EUR"])
    capital_eur: float = 300.0
    timeframe: str = "ONE_DAY"
    sma_regime: int = 200
    donchian_entry: int = 20
    donchian_exit: int = 10
    atr_period: int = 20
    atr_mult: float = 3.0
    risk_per_trade: float = 0.01
    max_asset_pct: float = 0.5
    entry_buffer: float = 0.003      # limite « marketable » à close × (1 + buffer)
    stop_limit_gap: float = 0.01     # en live : limite du stop à stop × (1 − gap)


@dataclass
class DcaCfg:
    """Poche cœur : achats programmés, jamais de vente."""
    enabled: bool = True
    monthly_eur: float = 100.0
    weights: dict[str, float] = field(default_factory=lambda: {"BTC-EUR": 0.7, "ETH-EUR": 0.3})
    weekday: int = 0                 # 0 = lundi
    hour: int = 9
    minute: int = 0
    tilt: bool = False
    reprice_minutes: int = 30
    max_wait_hours: int = 6


@dataclass
class PromotionCfg:
    auto: bool = True
    dca_min_days: int = 7
    trend_min_days: int = 30
    intraday_min_days: int = 30
    trend_min_oos_profit_factor: float = 1.2
    trend_max_oos_drawdown: float = 0.25
    max_live_capital_eur: float = 0.0   # 0 = aucun euro réel engagé tant que tu ne le fixes pas


@dataclass
class AiCfg:
    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: str = "high"
    daily_hour: int = 7
    daily_minute: int = 30
    monthly_budget_usd: float = 15.0
    max_questions_per_day: int = 20
    web_search_max_uses: int = 5


@dataclass
class WebCfg:
    cf_team_domain: str = ""     # ex. "monequipe.cloudflareaccess.com"
    cf_aud: str = ""             # tag d'audience de l'application Access
    host: str = "127.0.0.1"
    port: int = 8080


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
    data_dir: str = "data"
    notifications: bool = True
    timezone: str = "Europe/Paris"
    intraday_enabled: bool = False
    intraday_capital_eur: float = 300.0
    trend: TrendCfg = field(default_factory=TrendCfg)
    dca: DcaCfg = field(default_factory=DcaCfg)
    promotion: PromotionCfg = field(default_factory=PromotionCfg)
    ai: AiCfg = field(default_factory=AiCfg)
    web: WebCfg = field(default_factory=WebCfg)

    def path(self, name: str) -> str:
        return str(Path(self.data_dir) / name)

    @property
    def paper_journal_path(self) -> str:
        return self.journal_path.replace(".sqlite", "_paper.sqlite")

    @property
    def control_path(self) -> str:
        return self.path("control.sqlite")

    @property
    def market_cache_path(self) -> str:
        return self.path("market.sqlite")

    @property
    def all_products(self) -> list[str]:
        """Toutes les paires suivies (intraday, tendance, DCA), sans doublon, ordre stable."""
        out: list[str] = []
        for p in [*self.products, *self.trend.products, *self.dca.weights]:
            if p not in out:
                out.append(p)
        return out


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load(path: str | Path = "config.toml") -> Config:
    """Les chemins relatifs sont résolus depuis le dossier du fichier de config,
    pour que le bot fonctionne quel que soit le dossier courant (launchd, cron…)."""
    path = Path(path).expanduser().resolve()
    base = path.parent

    def here(p: str) -> str:
        return str((base / Path(p).expanduser()).resolve())

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    risk = dict(raw.get("risk", {}))
    risk["blackout"] = [(_dt(a), _dt(b)) for a, b in risk.get("blackout", [])]
    risk["kill_switch_file"] = here(risk.get("kill_switch_file", "data/STOP"))
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
        journal_path=here(raw.get("journal", {}).get("path", "data/journal.sqlite")),
        data_dir=here("data"),
        notifications=raw.get("macos", {}).get("notifications", True),
        timezone=raw.get("timezone", "Europe/Paris"),
        intraday_enabled=raw.get("sleeves", {}).get("intraday", {}).get("enabled", False),
        intraday_capital_eur=raw.get("sleeves", {}).get("intraday", {}).get("capital_eur", 300.0),
        trend=TrendCfg(**raw.get("sleeves", {}).get("trend", {})),
        dca=DcaCfg(**raw.get("sleeves", {}).get("dca", {})),
        promotion=PromotionCfg(**raw.get("promotion", {})),
        ai=AiCfg(**raw.get("ai", {})),
        web=WebCfg(**raw.get("web", {})),
    )
