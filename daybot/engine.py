"""Moteur commun backtest / paper / live : traite une bougie clôturée à la fois."""
from __future__ import annotations

import logging

import pandas as pd

from .config import Config
from .journal import Journal, Trade
from .risk import RiskManager
from .strategy import signal_at

log = logging.getLogger("daybot")


class Engine:
    def __init__(self, cfg: Config, broker, journal: Journal):
        self.cfg, self.broker, self.journal = cfg, broker, journal
        self.risk = RiskManager(cfg)
        self.prices: dict[str, float] = {}
        self.day = ""
        self.day_start_equity = 0.0

    def on_bar(self, product: str, df: pd.DataFrame, i: int) -> None:
        """`df` = features (strategy.features), `i` = index de la bougie clôturée."""
        bar = df.iloc[i]
        now = df.index[i]
        self.prices[product] = bar["close"]
        s = self.cfg.strategy

        for t in self.journal.active(product):
            if t.status == "PENDING":
                if self.broker.poll_entry(t, bar):
                    self._opened(t, bar)
                else:
                    t.bars_waiting = int(t.bars_waiting) + 1
                    if t.bars_waiting >= s.entry_ttl_bars:
                        if self.broker.cancel_entry(t):      # partiel conservé (live)
                            self._opened(t, bar, check_stop=False)
                        else:
                            t.status = "CANCELLED"
                            log.info("%s entrée annulée (non remplie)", product)
                self.journal.save(t)
            elif t.status == "OPEN":
                t.bars_held = int(t.bars_held) + 1
                if self.broker.poll_exit(t, bar):
                    self._closed(t)
                elif t.bars_held >= s.max_hold_bars:
                    self.broker.force_exit(t, bar["close"], now, "time")
                    self._closed(t)
                self.journal.save(t)

        equity = self.broker.equity(self.prices)
        self.risk.update_equity(equity)
        day = now.strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_start_equity = day, equity

        sig = signal_at(df, i, product, self.cfg)
        if sig is None or self.journal.active(product):
            return
        trades_today, pnl_today = self.journal.day_stats(day)
        dec = self.risk.approve(
            now=now.to_pydatetime(), entry=sig.entry, stop=sig.stop, equity=equity,
            available_quote=self.broker.available_quote(),
            open_positions=len(self.journal.active()), trades_today=trades_today,
            pnl_today=pnl_today, start_of_day_equity=self.day_start_equity)
        if not dec.ok:
            log.info("%s signal ignoré : %s", product, dec.reason)
            return
        t = self.journal.save(Trade(product, now.isoformat(), sig.entry, sig.stop,
                                    sig.target, dec.base_size))
        try:
            self.broker.place_entry(t)
            log.info("%s ACHAT limite %.4f stop %.4f TP %.4f taille %.6f (≈%.2f %s)",
                     product, sig.entry, sig.stop, sig.target, t.base_size,
                     dec.quote_size, self.cfg.quote_currency)
        except Exception as e:  # noqa: BLE001 — on journalise et on continue
            t.status = "CANCELLED"
            t.exit_reason = f"rejet: {e}"
            log.warning("%s ordre rejeté : %s", product, e)
        self.journal.save(t)

    def _opened(self, t: Trade, bar: pd.Series, check_stop: bool = True) -> None:
        t.status = "OPEN"
        self.broker.protect(t)
        log.info("%s position ouverte à %.4f", t.product, t.entry_price)
        # Stop touché dans la bougie même du remplissage (simulation conservatrice).
        if check_stop and self.broker.poll_exit(t, bar, same_bar=True):
            self._closed(t)

    def _closed(self, t: Trade) -> None:
        t.status = "CLOSED"
        log.info("%s sortie %s à %.4f — P&L net %.2f", t.product, t.exit_reason,
                 t.exit_price, t.pnl)
