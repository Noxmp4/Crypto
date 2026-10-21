"""Poche cœur : DCA hebdomadaire par ordres limites, sans jamais vendre.

- Budget hebdomadaire = mensuel × 12 / 52.
- Répartition : on achète d'abord ce qui est sous son poids cible (rééquilibrage
  sans vente, donc sans fait générateur fiscal en France).
- Exécution : limite post-only au meilleur bid (frais maker), reprix toutes les
  `reprice_minutes`, puis limite traversante au meilleur ask après `max_wait_hours`.
- Option « tilt » (désactivée par défaut) : ×1,5 sous la SMA200, ×0,75 au-dessus de
  1,5 × SMA200. L'IA n'a aucune prise sur cette poche.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from ..config import Config
from ..journal import DcaLot, Journal

log = logging.getLogger("daybot")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def allocate(budget: float, weights: dict[str, float], values: dict[str, float],
             min_order: float) -> dict[str, float]:
    """Répartit `budget` pour rapprocher le portefeuille de `weights`, sans vendre.
    Les parts inférieures à `min_order` sont regroupées sur les autres actifs."""
    if budget < min_order or not weights:
        return {}
    tw = sum(weights.values())
    w = {p: x / tw for p, x in weights.items()}
    total_after = sum(values.get(p, 0.0) for p in w) + budget
    deficit = {p: max(0.0, w[p] * total_after - values.get(p, 0.0)) for p in w}
    if sum(deficit.values()) <= 0:
        deficit = dict(w)
    while deficit:
        s = sum(deficit.values())
        alloc = {p: budget * d / s for p, d in deficit.items() if d > 0}
        small = [p for p, a in alloc.items() if a < min_order]
        if not small:
            return alloc
        deficit.pop(min(small, key=lambda p: alloc[p]))
    return {}


def tilt_factor(close: float, sma200: float | None) -> float:
    if sma200 is None or pd.isna(sma200) or sma200 <= 0:
        return 1.0
    if close < sma200:
        return 1.5
    if close > 1.5 * sma200:
        return 0.75
    return 1.0


class DcaSleeve:
    name = "dca"

    def __init__(self, cfg: Config, broker, journal: Journal, label: str = "dca",
                 deployable: Callable[[], float | None] | None = None,
                 allowed: Callable[[], bool] | None = None):
        self.cfg, self.broker, self.journal, self.label = cfg, broker, journal, label
        self.deployable = deployable or (lambda: None)
        self.allowed = allowed or (lambda: True)

    @property
    def weekly_budget(self) -> float:
        return self.cfg.dca.monthly_eur * 12 / 52

    def holdings_value(self, prices: dict[str, float]) -> dict[str, float]:
        return {p: h["qty"] * prices.get(p, h["avg_price"])
                for p, h in self.journal.dca_holdings().items()}

    def buy(self, prices: dict[str, float], sma200: dict[str, float] | None = None,
            now: datetime | None = None) -> list[DcaLot]:
        """Achat hebdomadaire. Renvoie les lots créés (ordres posés)."""
        d = self.cfg.dca
        if not self.allowed():
            log.info("[%s] achat DCA ignoré (poche non autorisée)", self.label)
            return []
        if self.journal.lots("PENDING"):
            log.info("[%s] achat DCA reporté : un achat précédent est encore en cours", self.label)
            return []
        budget = self.weekly_budget
        cap = self.deployable()
        if cap is not None:
            budget = min(budget, cap)
        budget = min(budget, self.broker.available_quote() * 0.99)
        alloc = allocate(budget, d.weights, self.holdings_value(prices),
                         self.cfg.risk.min_order_quote)
        if d.tilt and sma200:
            alloc = {p: a * tilt_factor(prices[p], sma200.get(p)) for p, a in alloc.items()}
            total = sum(alloc.values())
            limit = min(self.broker.available_quote() * 0.99, cap if cap is not None else total)
            if total > limit > 0:
                alloc = {p: a * limit / total for p, a in alloc.items()}
        lots = []
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for p, eur in alloc.items():
            if eur < self.cfg.risk.min_order_quote:
                continue
            bid, _ask = self.broker.best_bid_ask(p, prices[p])
            size = eur / bid / (1 + self.cfg.fees.maker)
            lot = self.journal.save_lot(DcaLot(p, stamp, eur, bid, size, placed_at=stamp))
            try:
                self.broker.place_lot(lot)
                log.info("[%s] DCA %s : %.2f € à %.4f (post-only)", self.label, p, eur, bid)
            except Exception as e:  # noqa: BLE001
                lot.status = "CANCELLED"
                log.warning("[%s] DCA %s rejeté : %s", self.label, p, e)
            self.journal.save_lot(lot)
            lots.append(lot)
        return lots

    def manage(self, prices: dict[str, float],
               bars: Callable[[str, str], pd.DataFrame | None] = lambda p, since: None,
               now: datetime | None = None) -> None:
        """Suivi des achats en cours : remplissage, reprix, bascule en limite traversante."""
        d = self.cfg.dca
        now = now or datetime.now(timezone.utc)
        for lot in self.journal.lots("PENDING"):
            if self.broker.poll_lot(lot, bars(lot.product, lot.placed_at)):
                lot.status = "FILLED"
                self.journal.save_lot(lot)
                log.info("[%s] DCA %s rempli : %.6f à %.4f", self.label, lot.product,
                         lot.base_size, lot.price)
                continue
            waited = now - _parse(lot.created)
            since_place = now - _parse(lot.placed_at)
            if lot.post_only and (waited >= timedelta(hours=d.max_wait_hours)
                                  or since_place >= timedelta(minutes=d.reprice_minutes)):
                if self.broker.cancel_lot(lot):          # partiel conservé (live)
                    lot.status = "FILLED"
                    self.journal.save_lot(lot)
                    continue
                bid, ask = self.broker.best_bid_ask(lot.product, prices.get(lot.product, 0.0))
                if waited >= timedelta(hours=d.max_wait_hours):
                    lot.post_only, lot.limit_price = 0, ask * 1.002
                    rate = self.cfg.fees.taker
                else:
                    lot.limit_price, rate = bid, self.cfg.fees.maker
                    lot.reprices = int(lot.reprices) + 1
                lot.base_size = lot.quote_amount / lot.limit_price / (1 + rate)
                lot.placed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    self.broker.place_lot(lot)
                except Exception as e:  # noqa: BLE001
                    lot.status = "CANCELLED"
                    log.warning("[%s] DCA %s : nouvel ordre rejeté : %s", self.label, lot.product, e)
                self.journal.save_lot(lot)
            elif not lot.post_only and since_place >= timedelta(hours=1):
                if self.broker.cancel_lot(lot):
                    lot.status = "FILLED"
                else:
                    lot.status = "CANCELLED"
                    log.warning("[%s] DCA %s abandonné (non rempli)", self.label, lot.product)
                self.journal.save_lot(lot)
