"""Moteur commun backtest / paper / live : traite une bougie clôturée à la fois.

Un Engine = une poche « à position » (intraday ou tendance) sur un broker (paper ou
live). Plusieurs Engines peuvent partager le même broker et le même journal : chaque
trade porte le nom de sa poche, et l'état du risque est conservé par poche.
"""
from __future__ import annotations

import logging
from typing import Callable

import pandas as pd

from .config import Config
from .journal import Journal, Trade
from .notify import notify
from .risk import Controls, RiskManager
from .strategies.base import Strategy

log = logging.getLogger("daybot")


class Engine:
    def __init__(self, cfg: Config, broker, journal: Journal, strategy: Strategy | None = None,
                 capital: float | None = None, risk_overrides: dict | None = None,
                 controls: Callable[[], Controls] | None = None,
                 deployable: Callable[[], float | None] | None = None, label: str = ""):
        if strategy is None:
            from .strategies.intraday import IntradayStrategy
            strategy = IntradayStrategy(cfg)
        self.cfg, self.broker, self.journal, self.strategy = cfg, broker, journal, strategy
        self.sleeve = strategy.name
        self.label = label or self.sleeve
        # capital fixe alloué à la poche (None = tout le capital du broker, mode v1)
        self.capital = capital
        self.controls = controls or Controls
        self.deployable = deployable or (lambda: None)
        self.risk = RiskManager(cfg, risk_overrides)
        self.prices: dict[str, float] = {}
        # Clés d'état sans préfixe pour la poche intraday (compatibilité v1).
        self._prefix = "" if self.sleeve == "intraday" else f"{self.sleeve}:"
        # État conservé entre redémarrages : sans ça, un crash remettrait à zéro
        # le drawdown max et la perte journalière.
        self.risk.peak_equity = float(journal.get_state(self._k("peak_equity"), "0"))
        self.day = journal.get_state(self._k("day"))
        self.day_start_equity = float(journal.get_state(self._k("day_start_equity"), "0"))
        self._halt_notified = False

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _notify(self, title: str, msg: str) -> None:
        notify(title, msg, self.cfg.notifications)

    def sleeve_equity(self) -> float:
        """Capital de la poche : alloué + P&L réalisé + latent des positions ouvertes."""
        if self.capital is None:
            return self.broker.equity(self.prices)
        realized = sum(t.pnl for t in self.journal.closed(self.sleeve))
        latent = sum((self.prices.get(t.product, t.entry_price) - t.entry_price) * t.base_size
                     - t.entry_fee for t in self.journal.active(sleeve=self.sleeve)
                     if t.status == "OPEN")
        return self.capital + realized + latent

    def on_bar(self, product: str, df: pd.DataFrame, i: int) -> None:
        """`df` = features de la stratégie, `i` = index de la bougie clôturée."""
        bar = df.iloc[i]
        now = df.index[i]
        self.prices[product] = bar["close"]
        st = self.strategy

        for t in self.journal.active(product, self.sleeve):
            if t.status == "PENDING":
                if self.broker.poll_entry(t, bar):
                    self._opened(t, bar)
                else:
                    t.bars_waiting = int(t.bars_waiting) + 1
                    if t.bars_waiting >= st.entry_ttl_bars:
                        if self.broker.cancel_entry(t):      # partiel conservé (live)
                            self._opened(t, bar, check_stop=False)
                        else:
                            t.status = "CANCELLED"
                            log.info("[%s] %s entrée annulée (non remplie)", self.label, product)
                self.journal.save(t)
            elif t.status == "OPEN":
                t.bars_held = int(t.bars_held) + 1
                if self.broker.poll_exit(t, bar):
                    self._closed(t)
                elif reason := st.exit_at(df, i, t):
                    self.broker.force_exit(t, bar["close"], now, reason)
                    self._closed(t)
                elif st.max_hold_bars and t.bars_held >= st.max_hold_bars:
                    self.broker.force_exit(t, bar["close"], now, "time")
                    self._closed(t)
                else:
                    new_stop = st.trail(df, i, t)
                    if new_stop and new_stop > t.stop:
                        old = t.stop
                        if self.broker.update_stop(t, new_stop):
                            log.info("[%s] %s stop relevé %.4f → %.4f", self.label, product,
                                     old, new_stop)
                        elif self.broker.poll_exit(t, bar):   # exécuté entre-temps (live)
                            self._closed(t)
                self.journal.save(t)

        equity = self.sleeve_equity()
        peak = self.risk.peak_equity
        self.risk.update_equity(equity)
        if self.risk.peak_equity != peak:
            self.journal.set_state(self._k("peak_equity"), self.risk.peak_equity)
        if self.risk.halted_reason and not self._halt_notified:
            self._halt_notified = True
            self._notify(f"daybot · {self.label} arrêté", self.risk.halted_reason)
        day = now.strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_start_equity = day, equity
            self.journal.set_state(self._k("day"), day)
            self.journal.set_state(self._k("day_start_equity"), equity)

        sig = st.signal_at(df, i, product)
        if sig is None or self.journal.active(product, self.sleeve):
            return
        trades_today, pnl_today = self.journal.day_stats(day, self.sleeve)
        dec = self.risk.approve(
            now=now.to_pydatetime(), entry=sig.entry, stop=sig.stop, equity=equity,
            available_quote=self.broker.available_quote(),
            open_positions=len(self.journal.active(sleeve=self.sleeve)),
            trades_today=trades_today, pnl_today=pnl_today,
            start_of_day_equity=self.day_start_equity, controls=self.controls(),
            sleeve=self.sleeve, deployable=self.deployable())
        if not dec.ok:
            log.info("[%s] %s signal ignoré : %s", self.label, product, dec.reason)
            return
        t = self.journal.save(Trade(product, now.isoformat(), sig.entry, sig.stop,
                                    sig.target, dec.base_size, sleeve=self.sleeve,
                                    initial_stop=sig.stop, entry_mode=st.entry_mode))
        try:
            self.broker.place_entry(t)
            log.info("[%s] %s ACHAT limite %.4f stop %.4f%s taille %.6f (≈%.2f %s)",
                     self.label, product, sig.entry, sig.stop,
                     f" TP {sig.target:.4f}" if sig.target else "", t.base_size,
                     dec.quote_size, self.cfg.quote_currency)
        except Exception as e:  # noqa: BLE001 — on journalise et on continue
            t.status = "CANCELLED"
            t.exit_reason = f"rejet: {e}"
            log.warning("[%s] %s ordre rejeté : %s", self.label, product, e)
        self.journal.save(t)

    def poll_live(self) -> None:
        """Entre deux bougies (live) : protège tout de suite une entrée remplie et
        enregistre les sorties exécutées par l'exchange."""
        for t in self.journal.active(sleeve=self.sleeve):
            if t.status == "PENDING" and self.broker.poll_entry(t, None):
                self._opened(t, None, check_stop=False)
                self.journal.save(t)
            elif t.status == "OPEN" and self.broker.poll_exit(t, None):
                self._closed(t)
                self.journal.save(t)

    def close_now(self, t: Trade, price: float, reason: str = "manual") -> None:
        """Clôture demandée depuis le site web."""
        self.broker.force_exit(t, price, pd.Timestamp.now(tz="UTC"), reason)
        self._closed(t)
        self.journal.save(t)

    def _opened(self, t: Trade, bar: pd.Series | None, check_stop: bool = True) -> None:
        t.status = "OPEN"
        self.broker.protect(t)
        log.info("[%s] %s position ouverte à %.4f", self.label, t.product, t.entry_price)
        target = f", objectif {t.target:.4f}" if t.has_target else ""
        self._notify(f"daybot · {t.product}",
                     f"Achat rempli à {t.entry_price:.4f} — stop {t.stop:.4f}{target}")
        # Stop touché dans la bougie même du remplissage (simulation conservatrice).
        if check_stop and bar is not None and self.broker.poll_exit(t, bar, same_bar=True):
            self._closed(t)

    def _closed(self, t: Trade) -> None:
        t.status = "CLOSED"
        log.info("[%s] %s sortie %s à %.4f — P&L net %.2f", self.label, t.product,
                 t.exit_reason, t.exit_price, t.pnl)
        self._notify(f"daybot · {t.product}", f"Sortie {t.exit_reason} à {t.exit_price:.4f} — "
                     f"P&L net {t.pnl:+.2f} {self.cfg.quote_currency}")
