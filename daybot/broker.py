"""Exécution : PaperBroker (simulation, aussi utilisé par le backtest) et LiveBroker.

Interface commune, appelée par l'Engine à chaque bougie clôturée :
  place_entry(trade)              ordre limite d'achat (post-only ou « marketable »)
  poll_entry(trade, bar) -> bool  True si rempli (renseigne entry_*)
  cancel_entry(trade)
  protect(trade)                  pose la protection sur l'exchange en live :
                                  bracket TP + stop si le trade a un objectif,
                                  sinon stop-limit seul (stop suiveur)
  update_stop(trade, stop)        relève le stop (stop suiveur)
  poll_exit(trade, bar) -> bool   True si sorti (renseigne exit_*)
  force_exit(trade, price)        sortie au marché (durée max, signal, arrêt)
  place_lot / poll_lot / cancel_lot   achats DCA (jamais de vente)
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


CLIENT_PREFIX = "daybot-"


def _iso(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coid() -> str:
    """client_order_id reconnaissable : la réconciliation n'annule que les ordres du bot."""
    return f"{CLIENT_PREFIX}{uuid.uuid4().hex}"


class PaperBroker:
    """Simulation conservatrice : remplissage limite seulement si le prix passe
    *strictement* à travers ; si stop et TP sont touchés dans la même bougie,
    on suppose le stop ; les gaps sous le stop sont exécutés à l'ouverture."""

    def __init__(self, cfg: Config, balance: float | None = None):
        self.cfg = cfg
        self.quote = balance if balance is not None else cfg.paper_balance
        self.reserved: dict[int, float] = {}
        self.lot_reserved: dict[int, float] = {}
        self.base: dict[str, float] = {}
        self.total_fees = 0.0

    def _entry_fee_rate(self, t: Trade) -> float:
        return self.cfg.fees.taker if t.entry_mode == "marketable" else self.cfg.fees.maker

    def restore(self, journal) -> None:
        """Reconstruit soldes et positions depuis le journal (redémarrage, reboot)."""
        self.quote = self.cfg.paper_balance
        self.reserved, self.lot_reserved, self.base, self.total_fees = {}, {}, {}, 0.0
        for t in journal.closed():
            self.quote += t.pnl
            self.total_fees += t.entry_fee + t.exit_fee
        for t in journal.active():
            if t.status == "PENDING":
                cost = t.entry_limit * t.base_size * (1 + self._entry_fee_rate(t))
                self.quote -= cost
                self.reserved[t.id] = cost
            else:
                self.quote -= t.entry_price * t.base_size + t.entry_fee
                self.base[t.product] = self.base.get(t.product, 0.0) + t.base_size
                self.total_fees += t.entry_fee
        for lot in journal.lots():
            if lot.status == "FILLED":
                self.quote -= lot.cost
                self.base[lot.product] = self.base.get(lot.product, 0.0) + lot.base_size
                self.total_fees += lot.fee
            elif lot.status == "PENDING":
                cost = self._lot_cost(lot)
                self.quote -= cost
                self.lot_reserved[lot.id] = cost

    def available_quote(self) -> float:
        return self.quote

    def equity(self, prices: dict[str, float]) -> float:
        return self.quote + sum(self.reserved.values()) + sum(self.lot_reserved.values()) + sum(
            q * prices.get(p, 0.0) for p, q in self.base.items())

    def place_entry(self, t: Trade) -> None:
        cost = t.entry_limit * t.base_size * (1 + self._entry_fee_rate(t))
        if cost > self.quote:
            raise ValueError("solde EUR insuffisant")
        self.quote -= cost
        self.reserved[t.id] = cost
        t.entry_order_id = f"paper-{uuid.uuid4().hex[:8]}"

    def poll_entry(self, t: Trade, bar: pd.Series | None) -> bool:
        if bar is None:                 # la simulation ne remplit que sur une bougie
            return False
        if t.entry_mode == "marketable":
            # Limite traversante : exécutée à l'ouverture si elle est sous la limite.
            if bar["open"] <= t.entry_limit:
                px = min(bar["open"] * (1 + self.cfg.fees.slippage), t.entry_limit)
            elif bar["low"] < t.entry_limit:
                px = t.entry_limit
            else:
                return False
        elif bar["low"] < t.entry_limit:
            px = t.entry_limit
        else:
            return False
        reserved = self.reserved.pop(t.id)
        t.entry_price = px
        t.entry_fee = px * t.base_size * self._entry_fee_rate(t)
        t.entry_time = _iso(bar.name)
        self.quote += reserved - (px * t.base_size + t.entry_fee)   # rend l'excédent réservé
        self.base[t.product] = self.base.get(t.product, 0.0) + t.base_size
        self.total_fees += t.entry_fee
        return True

    def cancel_entry(self, t: Trade) -> None:
        self.quote += self.reserved.pop(t.id, 0.0)

    def protect(self, t: Trade) -> None:
        t.exit_order_id = f"paper-bracket-{t.id}"

    def update_stop(self, t: Trade, stop: float) -> bool:
        t.stop = stop
        return True

    def poll_exit(self, t: Trade, bar: pd.Series | None, same_bar: bool = False) -> bool:
        if bar is None:
            return False
        f = self.cfg.fees
        if bar["low"] <= t.stop:
            px = min(t.stop, bar["open"]) * (1 - f.slippage)
            return self._close(t, px, f.taker, "stop", bar.name)
        if not same_bar and t.has_target and bar["high"] > t.target:
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

    # ── DCA ──
    def _lot_cost(self, lot) -> float:
        rate = self.cfg.fees.maker if lot.post_only else self.cfg.fees.taker
        return lot.limit_price * lot.base_size * (1 + rate)

    def best_bid_ask(self, product: str, last: float) -> tuple[float, float]:
        return last, last

    def place_lot(self, lot) -> None:
        cost = self._lot_cost(lot)
        if cost > self.quote + 1e-9:
            raise ValueError("solde EUR insuffisant")
        self.quote -= cost
        self.lot_reserved[lot.id] = cost
        lot.order_id = f"paper-lot-{uuid.uuid4().hex[:8]}"

    def poll_lot(self, lot, bars: pd.DataFrame | None) -> bool:
        if bars is None or bars.empty:
            return False
        if lot.post_only:
            if not (bars["low"] < lot.limit_price).any():
                return False
            px, rate = lot.limit_price, self.cfg.fees.maker
        else:
            px = min(bars["open"].iloc[0] * (1 + self.cfg.fees.slippage), lot.limit_price)
            rate = self.cfg.fees.taker
        reserved = self.lot_reserved.pop(lot.id)
        lot.price, lot.fee = px, px * lot.base_size * rate
        lot.filled_time = _iso(bars.index[-1])
        self.quote += reserved - lot.cost
        self.base[lot.product] = self.base.get(lot.product, 0.0) + lot.base_size
        self.total_fees += lot.fee
        return True

    def cancel_lot(self, lot) -> bool:
        self.quote += self.lot_reserved.pop(lot.id, 0.0)
        return False


class LiveBroker:
    """Ordres réels via le SDK officiel `coinbase-advanced-py`.

    Clés (clé CDP « Trade » uniquement — JAMAIS la permission « Transfer ») :
    COINBASE_KEY_FILE, COINBASE_API_KEY / COINBASE_API_SECRET, ou le Trousseau
    macOS (voir daybot/credentials.py).
    La protection est posée sur l'exchange dès le remplissage (bracket TP + stop, ou
    stop-limit pour les stops suiveurs) : la position reste protégée si le bot plante.
    Tous les ordres portent un client_order_id « daybot-… » (voir `reconcile`).
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
            client_order_id=_coid(), product_id=t.product, base_size=size,
            limit_price=self.round_price(t.product, t.entry_limit),
            post_only=t.entry_mode != "marketable", **self._kw())
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
        size = self.round_base(t.product, t.base_size)
        if t.has_target:
            resp = self.client.trigger_bracket_order_gtc_sell(
                client_order_id=_coid(), product_id=t.product, base_size=size,
                limit_price=self.round_price(t.product, t.target),
                stop_trigger_price=self.round_price(t.product, t.stop), **self._kw())
            t.exit_order_id = self._check(resp, "ordre bracket")
            return
        # Stop suiveur : stop-limit avec une limite un peu sous le déclencheur, pour
        # maximiser les chances d'exécution dans un mouvement rapide.
        gap = self.cfg.trend.stop_limit_gap
        resp = self.client.stop_limit_order_gtc_sell(
            client_order_id=_coid(), product_id=t.product, base_size=size,
            limit_price=self.round_price(t.product, t.stop * (1 - gap)),
            stop_price=self.round_price(t.product, t.stop),
            stop_direction="STOP_DIRECTION_STOP_DOWN", **self._kw())
        t.exit_order_id = self._check(resp, "ordre stop")

    def update_stop(self, t: Trade, stop: float) -> bool:
        """Annule puis remplace le stop. False si l'ancien stop a été exécuté entre-temps."""
        if t.exit_order_id:
            self.client.cancel_orders(order_ids=[t.exit_order_id])
            time.sleep(1)
            if self._order(t.exit_order_id)["status"] == "FILLED":
                return False
        t.stop = stop
        self.protect(t)
        return True

    def poll_exit(self, t: Trade, bar=None, same_bar: bool = False) -> bool:
        if not t.exit_order_id:
            return False
        o = self._order(t.exit_order_id)
        if o["status"] != "FILLED":
            return False
        t.exit_price = float(o["average_filled_price"])
        t.exit_fee = float(o.get("total_fees") or 0)
        t.exit_time = o.get("last_fill_time") or _iso(pd.Timestamp.now(tz="UTC"))
        t.exit_reason = ("take_profit" if t.has_target and t.exit_price >= t.target * 0.999
                         else "stop")
        return True

    def force_exit(self, t: Trade, price: float, when, reason: str = "time") -> None:
        if t.exit_order_id:
            self.client.cancel_orders(order_ids=[t.exit_order_id])
            time.sleep(1)
            if self.poll_exit(t):          # rempli entre-temps
                return
        resp = self.client.market_order_sell(
            client_order_id=_coid(), product_id=t.product,
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

    # ── DCA ──
    def best_bid_ask(self, product: str, last: float = 0.0) -> tuple[float, float]:
        d = self._d(self.client.get_best_bid_ask(product_ids=[product]))
        book = (d.get("pricebooks") or [{}])[0]
        bid = float(book["bids"][0]["price"]) if book.get("bids") else last
        ask = float(book["asks"][0]["price"]) if book.get("asks") else last
        return bid, ask

    def place_lot(self, lot) -> None:
        size = self.round_base(lot.product, lot.base_size)
        if Decimal(size) < Decimal(self.spec(lot.product)["base_min_size"]):
            raise ValueError(f"taille {size} < minimum {self.spec(lot.product)['base_min_size']}")
        lot.base_size = float(size)
        resp = self.client.limit_order_gtc_buy(
            client_order_id=_coid(), product_id=lot.product, base_size=size,
            limit_price=self.round_price(lot.product, lot.limit_price),
            post_only=bool(lot.post_only), **self._kw())
        lot.order_id = self._check(resp, "achat DCA")

    def _fill_lot(self, lot, o: dict) -> None:
        lot.price = float(o["average_filled_price"])
        lot.base_size = float(o["filled_size"])
        lot.fee = float(o.get("total_fees") or 0)
        lot.filled_time = o.get("last_fill_time") or _iso(pd.Timestamp.now(tz="UTC"))

    def poll_lot(self, lot, bars=None) -> bool:
        o = self._order(lot.order_id)
        if o["status"] != "FILLED":
            return False
        self._fill_lot(lot, o)
        return True

    def cancel_lot(self, lot) -> bool:
        """Annule ; True si un remplissage partiel est conservé (lot alors FILLED)."""
        self.client.cancel_orders(order_ids=[lot.order_id])
        time.sleep(1)
        o = self._order(lot.order_id)
        if float(o.get("filled_size") or 0) > 0:
            self._fill_lot(lot, o)
            return True
        return False

    # ── exploitation ──
    def refresh_fees(self) -> dict:
        """Met à jour les frais réels (palier selon le volume 30 j)."""
        d = self._d(self.client.get_transaction_summary(product_type="SPOT"))
        tier = d.get("fee_tier") or {}
        if tier.get("maker_fee_rate"):
            self.cfg.fees.maker = float(tier["maker_fee_rate"])
            self.cfg.fees.taker = float(tier["taker_fee_rate"])
        return tier

    def open_orders(self) -> list[dict]:
        out, cursor = [], None
        while True:
            kw = {"order_status": ["OPEN"], "limit": 250, **self._kw()}
            if cursor:
                kw["cursor"] = cursor
            d = self._d(self.client.list_orders(**kw))
            out += d.get("orders", [])
            if not d.get("has_next"):
                return out
            cursor = d.get("cursor")

    def reconcile(self, journal) -> list[str]:
        """Compare le journal et l'exchange. Renvoie la liste des corrections faites.

        - ordre « daybot-… » ouvert sur l'exchange mais inconnu du journal → annulé ;
        - position OPEN dont la protection n'est plus ouverte → exécutée (on clôture)
          ou annulée/expirée (on la repose) ;
        - entrée PENDING remplie pendant un arrêt → laissée à l'Engine (poll_live).
        """
        notes: list[str] = []
        orders = self.open_orders()
        open_ids = {o["order_id"] for o in orders}
        known = {t.entry_order_id for t in journal.active()} | {
            t.exit_order_id for t in journal.active()} | {
            lot.order_id for lot in journal.lots("PENDING")}
        for o in orders:
            if o.get("client_order_id", "").startswith(CLIENT_PREFIX) and o["order_id"] not in known:
                self.client.cancel_orders(order_ids=[o["order_id"]])
                notes.append(f"ordre orphelin annulé {o.get('product_id')} {o['order_id']}")
        for t in journal.active():
            if t.status != "OPEN" or (t.exit_order_id and t.exit_order_id in open_ids):
                continue
            if t.exit_order_id and self.poll_exit(t):
                t.status = "CLOSED"
                journal.save(t)
                notes.append(f"{t.product} : sortie {t.exit_reason} exécutée pendant l'arrêt")
                continue
            self.protect(t)
            journal.save(t)
            notes.append(f"{t.product} : protection reposée (stop {t.stop:.4f})")
        return notes
