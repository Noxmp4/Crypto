"""Gestion du risque : sizing et garde-fous. Toute entrée passe par `approve`."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config, RiskCfg


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    quote_size: float = 0.0
    base_size: float = 0.0


@dataclass
class Controls:
    """État piloté depuis le site web et par l'analyste IA, relu à chaque bougie.

    L'IA ne peut que *réduire* le risque : `risk_multiplier` ≤ 1 et des blackouts
    supplémentaires. Rien ici ne permet d'augmenter une taille de position."""
    paused: bool = False
    entries_blocked: set[str] = field(default_factory=set)   # poches rétrogradées / coupées
    risk_multiplier: float = 1.0
    blackouts: list[tuple[datetime, datetime, str]] = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg: Config, overrides: dict | None = None):
        self.cfg = cfg
        # Limites propres à une poche (ex. tendance : 1 % par trade, drawdown toléré plus large).
        self.r: RiskCfg = dataclasses.replace(cfg.risk, **(overrides or {}))
        self.peak_equity = 0.0
        self.halted_reason = ""

    def update_equity(self, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        r = self.r
        if self.peak_equity and equity <= self.peak_equity * (1 - r.max_drawdown):
            self.halted_reason = f"drawdown max {r.max_drawdown:.0%} atteint — entrées arrêtées"

    def approve(self, *, now: datetime, entry: float, stop: float, equity: float,
                available_quote: float, open_positions: int, trades_today: int,
                pnl_today: float, start_of_day_equity: float,
                controls: Controls | None = None, sleeve: str = "",
                deployable: float | None = None) -> Decision:
        r = self.r
        c = controls or Controls()
        if self.halted_reason:
            return Decision(False, self.halted_reason)
        if Path(r.kill_switch_file).exists():
            return Decision(False, f"kill switch présent ({r.kill_switch_file})")
        if c.paused:
            return Decision(False, "pause manuelle (site web)")
        if sleeve and sleeve in c.entries_blocked:
            return Decision(False, f"entrées bloquées pour la poche {sleeve}")
        for a, b in r.blackout:
            if a <= now <= b:
                return Decision(False, "fenêtre macro (blackout)")
        for a, b, why in c.blackouts:
            if a <= now <= b:
                return Decision(False, f"blackout IA : {why}")
        if c.risk_multiplier <= 0:
            return Decision(False, "risque coupé par l'analyse IA (multiplicateur 0)")
        if open_positions >= r.max_open_positions:
            return Decision(False, "nombre max de positions ouvertes")
        if trades_today >= r.max_trades_per_day:
            return Decision(False, "nombre max de trades du jour")
        if start_of_day_equity and pnl_today <= -r.max_daily_loss * start_of_day_equity:
            return Decision(False, "perte journalière max atteinte")
        stop_frac = (entry - stop) / entry
        if stop_frac <= 0:
            return Decision(False, "stop invalide")
        # Sizing : perte au stop ≈ risk_per_trade × capital (formule du skill
        # crypto-spot-futures-analysis : notional = risque / distance du stop).
        quote = equity * r.risk_per_trade * min(c.risk_multiplier, 1.0) / stop_frac
        quote = min(quote, equity * r.max_position_pct, available_quote * 0.995)
        if deployable is not None:          # plafond d'euros réels engagés
            quote = min(quote, deployable)
        if quote < r.min_order_quote:
            return Decision(False, f"taille {quote:.2f} < minimum {r.min_order_quote}")
        return Decision(True, "", quote, quote / entry)
