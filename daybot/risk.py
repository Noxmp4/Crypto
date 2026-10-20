"""Gestion du risque : sizing et garde-fous. Toute entrée passe par `approve`."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    quote_size: float = 0.0
    base_size: float = 0.0


class RiskManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.peak_equity = 0.0
        self.halted_reason = ""

    def update_equity(self, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        r = self.cfg.risk
        if self.peak_equity and equity <= self.peak_equity * (1 - r.max_drawdown):
            self.halted_reason = f"drawdown max {r.max_drawdown:.0%} atteint — bot arrêté"

    def approve(self, *, now: datetime, entry: float, stop: float, equity: float,
                available_quote: float, open_positions: int, trades_today: int,
                pnl_today: float, start_of_day_equity: float) -> Decision:
        r = self.cfg.risk
        if self.halted_reason:
            return Decision(False, self.halted_reason)
        if Path(r.kill_switch_file).exists():
            return Decision(False, f"kill switch présent ({r.kill_switch_file})")
        for a, b in r.blackout:
            if a <= now <= b:
                return Decision(False, "fenêtre macro (blackout)")
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
        quote = equity * r.risk_per_trade / stop_frac
        quote = min(quote, equity * r.max_position_pct, available_quote * 0.995)
        if quote < r.min_order_quote:
            return Decision(False, f"taille {quote:.2f} < minimum {r.min_order_quote}")
        return Decision(True, "", quote, quote / entry)
