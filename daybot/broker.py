"""Exécution : PaperBroker (simulation, aussi utilisé par le backtest) et LiveBroker.

Interface commune, appelée par l'Engine à chaque bougie clôturée :
  place_entry(trade)              ordre limite d'achat post-only (maker)
  poll_entry(trade, bar) -> bool  True si rempli (renseigne entry_*)
  cancel_entry(trade)
  protect(trade)                  pose TP + stop (bracket côté exchange en live)
  poll_exit(trade, bar) -> bool   True si sorti (renseigne exit_*)
  force_exit(trade, price)        sortie au marché (durée max, arrêt)
  equity(prices) / available_quote()
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal

import pandas as pd

from . import credentials
from .config import Config
from .journal import Trade


def _iso(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


class PaperBroker:
    """Simulation conservatrice : remplissage limite seulement si le prix passe
    *strictement* à travers ; si stop et TP sont touchés dans la même bougie,
    on suppose le stop ; les gaps sous le stop sont exécutés à l'ouverture."""

    def __init__(self, cfg: Config, balance: float | None = None):
        self.cfg = cfg
        self.quote = balance if balance is not None else cfg.paper_balance
        self.reserved: dict[int, float] = {}
        self.base: dict[str, float] = {}
        self.total_fees = 0.0

    def restore(self, journal) -> None:
        """Reconstruit soldes et positions depuis le journal (redémarrage launchd, reboot)."""
        self.quote = self.cfg.paper_balance
        self.reserved, self.base, self.total_fees = {}, {}, 0.0
        for t in journal.closed():
            self.quote += t.pnl
            self.total_fees += t.entry_fee + t.exit_fee
        for t in journal.active():
            if t.status == "PENDING":
                cost = t.entry_limit * t.base_size * (1 + self.cfg.fees.maker)
                self.quote -= cost
                self.reserved[t.id] = cost
            else:
                self.quote -= t.entry_price * t.base_size + t.entry_fee
                self.base[t.product] = self.base.get(t.product, 0.0) + t.base_size
                self.total_fees += t.entry_fee

    def available_quote(self) -> float:
        return self.quote

    def equity(self, prices: dict[str, float]) -> float:
        return self.quote + sum(self.reserved.values()) + sum(
            q * prices.get(p, 0.0) for p, q in self.base.items())

    def place_entry(self, t: Trade) -> None:
        cost = t.entry_limit * t.base_size * (1 + self.cfg.fees.maker)
        if cost > self.quote:
            raise ValueError("solde EUR insuffisant")
        self.quote -= cost
        self.reserved[t.id] = cost
        t.entry_order_id = f"paper-{uuid.uuid4().hex[:8]}"

    def poll_entry(self, t: Trade, bar: pd.Series) -> bool:
        if bar["low"] >= t.entry_limit:
            return False
        self.reserved.pop(t.id)
        t.entry_price = t.entry_limit
        t.entry_fee = t.entry_price * t.base_size * self.cfg.fees.maker
        t.entry_time = _iso(bar.name)
        self.base[t.product] = self.base.get(t.product, 0.0) + t.base_size
        self.total_fees += t.entry_fee
        return True

    def cancel_entry(self, t: Trade) -> None:
        self.quote += self.reserved.pop(t.id, 0.0)

    def protect(self, t: Trade) -> None:
        t.exit_order_id = f"paper-bracket-{t.id}"

    def poll_exit(self, t: Trade, bar: pd.Series, same_bar: bool = False) -> bool:
        f = self.cfg.fees
        if bar["low"] <= t.stop:
            px = min(t.stop, bar["open"]) * (1 - f.slippage)
            return self._close(t, px, f.taker, "stop", bar.name)
        if not same_bar and bar["high"] > t.target:
            return self._close(t, t.target, f.maker, "take_profit", bar.name)
        return False

    def force_exit(self, t: Trade, price: float, when, reason: str = "time") -> None:
        f = self.cfg.fees
        self._close(t, price * (1 - f.slippage), f.taker, reason, when)

    def _close(self, t: Trade, px: float, fee_rate: float, reason: str, when) -> bool:
        t.exit_price, t.exit_reason, t.exit_time = px, reason, _iso(when)
        t.exit_fee = px * t.base_size * fee_rate
        self.quote += px * t.base_size - t.exit_fee
        self.base[t.product] -= t.base_size
        self.total_fees += t.exit_fee
        return True


class LiveBroker:
    """Ordres réels via le SDK officiel `coinbase-advanced-py`.

    Clés (clé CDP « Trade » uniquement — JAMAIS la permission « Transfer ») :
    COINBASE_KEY_FILE, COINBASE_API_KEY / COINBASE_API_SECRET, ou le Trousseau
    macOS (voir daybot/credentials.py).
    Le TP et le stop sont posés sur l'exchange (ordre bracket) dès le remplissage :
    la position reste protégée même si le bot plante.
    """

    def __init__(self, cfg: Config):
        from coinbase.rest import RESTClient

        self.cfg = cfg
        self.client = RESTClient(**credentials.credentials(), timeout=20)
        self._spec: dict[str, dict] = {}

    # ── utilitaires ──
    @staticmethod
    def _d(resp) -> dict:
        return resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)

    def _kw(self) -> dict:
        return {"retail_portfolio_id": self.cfg.portfolio_id} if self.cfg.portfolio_id else {}

    def spec(self, product: str) -> dict:
        if product not in self._spec:
            self._spec[product] = self._d(self.client.get_product(product))
        return self._spec[product]

    def round_base(self, product: str, size: float) -> str:
        inc = Decimal(self.spec(product)["base_increment"])
        return str((Decimal(str(size)) // inc) * inc)

    def round_price(self, product: str, price: float) -> str:
        inc = Decimal(self.spec(product)["quote_increment"])
        return str((Decimal(str(price)) // inc) * inc)

    def _order(self, order_id: str) -> dict:
        return self._d(self.client.get_order(order_id))["order"]

    def _check(self, resp, what: str) -> str:
        d = self._d(resp)
        if not d.get("success"):
            raise RuntimeError(f"{what} refusé : {d.get('error_response') or d}")
        return d["success_response"]["order_id"]

    # ── comptes ──
    def _accounts(self) -> list[dict]:
        out, cursor = [], None
        while True:
            kw = {"limit": 250, **({"cursor": cursor} if cursor else {}), **self._kw()}
            d = self._d(self.client.get_accounts(**kw))
            out += d.get("accounts", [])
            if not d.get("has_next"):
                return out
            cursor = d.get("cursor")

    def available_quote(self) -> float:
        for a in self._accounts():
            if a["currency"] == self.cfg.quote_currency:
                return float(a["available_balance"]["value"])
        return 0.0

    def equity(self, prices: dict[str, float]) -> float:
        """Capital alloué au bot : EUR + actifs des paires configurées."""
        bases = {p.split("-")[0]: prices.get(p, 0.0) for p in self.cfg.products}
        total = 0.0
        for a in self._accounts():
            qty = float(a["available_balance"]["value"]) + float(a["hold"]["value"])
            if a["currency"] == self.cfg.quote_currency:
                total += qty
            elif a["currency"] in bases:
                total += qty * bases[a["currency"]]
        return total

    # ── ordres ──
    def place_entry(self, t: Trade) -> None:
        size = self.round_base(t.product, t.base_size)
        if Decimal(size) < Decimal(self.spec(t.product)["base_min_size"]):
            raise ValueError(f"taille {size} < minimum {self.spec(t.product)['base_min_size']}")
        t.base_size = float(size)
        resp = self.client.limit_order_gtc_buy(
            client_order_id=str(uuid.uuid4()), product_id=t.product, base_size=size,
            limit_price=self.round_price(t.product, t.entry_limit), post_only=True, **self._kw())
        t.entry_order_id = self._check(resp, "ordre d'entrée")

    def poll_entry(self, t: Trade, bar=None) -> bool:
        o = self._order(t.entry_order_id)
        if o["status"] != "FILLED":
            return False
        self._fill_entry(t, o)
        return True

    def _fill_entry(self, t: Trade, o: dict) -> None:
        t.entry_price = float(o["average_filled_price"])
        t.base_size = float(o["filled_size"])
        t.entry_fee = float(o.get("total_fees") or 0)
        t.entry_time = o.get("last_fill_time") or _iso(pd.Timestamp.now(tz="UTC"))

    def cancel_entry(self, t: Trade) -> bool:
        """Annule ; renvoie True si un remplissage partiel doit être conservé."""
        self.client.cancel_orders(order_ids=[t.entry_order_id])
        time.sleep(1)
        o = self._order(t.entry_order_id)
        if float(o.get("filled_size") or 0) > 0:
            self._fill_entry(t, o)
            return True
        return False

    def protect(self, t: Trade) -> None:
        resp = self.client.trigger_bracket_order_gtc_sell(
            client_order_id=str(uuid.uuid4()), product_id=t.product,
            base_size=self.round_base(t.product, t.base_size),
            limit_price=self.round_price(t.product, t.target),
            stop_trigger_price=self.round_price(t.product, t.stop), **self._kw())
        t.exit_order_id = self._check(resp, "ordre bracket")

    def poll_exit(self, t: Trade, bar=None, same_bar: bool = False) -> bool:
        o = self._order(t.exit_order_id)
        if o["status"] != "FILLED":
            return False
        t.exit_price = float(o["average_filled_price"])
        t.exit_fee = float(o.get("total_fees") or 0)
        t.exit_time = o.get("last_fill_time") or _iso(pd.Timestamp.now(tz="UTC"))
        t.exit_reason = "take_profit" if t.exit_price >= t.target * 0.999 else "stop"
        return True

    def force_exit(self, t: Trade, price: float, when, reason: str = "time") -> None:
        if t.exit_order_id:
            self.client.cancel_orders(order_ids=[t.exit_order_id])
            time.sleep(1)
            if self.poll_exit(t):          # rempli entre-temps
                return
        resp = self.client.market_order_sell(
            client_order_id=str(uuid.uuid4()), product_id=t.product,
            base_size=self.round_base(t.product, t.base_size), **self._kw())
        oid = self._check(resp, "vente au marché")
        for _ in range(10):
            o = self._order(oid)
            if o["status"] == "FILLED":
                break
            time.sleep(1)
        t.exit_order_id = oid
        t.exit_price = float(o.get("average_filled_price") or price)
        t.exit_fee = float(o.get("total_fees") or 0)
        t.exit_time = o.get("last_fill_time") or _iso(when)
        t.exit_reason = reason
