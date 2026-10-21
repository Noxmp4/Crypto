"""Tests v2 : cache marché, migration du journal, tendance, DCA, promotion, réconciliation."""
import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from daybot import market, promotion
from daybot.broker import LiveBroker, PaperBroker
from daybot.config import Config
from daybot.engine import Engine
from daybot.journal import DcaLot, Journal, Trade
from daybot.risk import Controls, RiskManager
from daybot.sleeves.dca import DcaSleeve, allocate, tilt_factor
from daybot.strategies.trend import TrendStrategy, trend_risk_overrides

UTC = timezone.utc


def _cfg(**kw):
    cfg = Config(products=["TEST-EUR"])
    cfg = dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, kill_switch_file="/nonexistent"))
    cfg.trend = dataclasses.replace(cfg.trend, products=["TEST-EUR"], **kw)
    return cfg


def _bar(o, h, l, c, ts="2026-10-20T00:00Z"):
    return pd.Series({"open": o, "high": h, "low": l, "close": c}, name=pd.Timestamp(ts))


# ── cache marché ──

def test_history_cache_is_incremental(tmp_path, monkeypatch):
    calls = []
    day = 86400
    now = 1_800_000_000 - 1_800_000_000 % day + 3600       # 1 h après minuit UTC

    def fake_get(path, params, retries=4):
        calls.append(params)
        s, e = params["start"], params["end"]
        return {"candles": [{"start": str(t), "open": "1", "high": "2", "low": "0.5",
                             "close": "1.5", "volume": "3"} for t in range(s, e, day)]}

    monkeypatch.setattr(market, "_get", fake_get)
    path = str(tmp_path / "m.sqlite")
    df = market.history("X-EUR", "ONE_DAY", now - 10 * day, cache_path=path, now=now)
    assert len(df) == 10 and df.index[-1] == pd.Timestamp(now - 3600 - day, unit="s", tz="UTC")
    n = len(calls)
    df2 = market.history("X-EUR", "ONE_DAY", now - 10 * day, cache_path=path, now=now)
    assert len(df2) == 10 and len(calls) == n             # rien retéléchargé
    market.history("X-EUR", "ONE_DAY", now - 10 * day, cache_path=path, now=now + 2 * day)
    assert len(calls) == n + 1                             # seulement la fin manquante


# ── journal ──

def test_journal_migrates_v1_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, product TEXT, signal_time TEXT, "
               "entry_limit REAL, stop REAL, target REAL, base_size REAL, status TEXT, "
               "entry_order_id TEXT, entry_price REAL, entry_fee REAL, entry_time TEXT, "
               "exit_order_id TEXT, exit_price REAL, exit_fee REAL, exit_time TEXT, "
               "exit_reason TEXT, bars_waiting REAL, bars_held REAL)")
    db.execute("INSERT INTO trades (product, status, target, stop, base_size) "
               "VALUES ('BTC-EUR', 'OPEN', 110, 90, 1)")
    db.commit(); db.close()
    j = Journal(str(path))
    t = j.active()[0]
    assert t.sleeve == "intraday" and t.entry_mode == "maker" and t.has_target
    assert Journal(str(path)).active()[0].id == t.id      # migration idempotente


def test_dca_holdings_and_commands():
    j = Journal()
    for px in (100.0, 200.0):
        j.save_lot(DcaLot("BTC-EUR", "x", 10, px, 0.1, status="FILLED", price=px, fee=0.05))
    h = j.dca_holdings()["BTC-EUR"]
    assert h["qty"] == pytest.approx(0.2) and h["avg_price"] == pytest.approx(30.1 / 0.2)
    cid = j.add_command("pause", {"x": 1}, "moi@example.com")
    assert [r["id"] for r in j.pending_commands()] == [cid]
    j.finish_command(cid, True, "ok")
    assert j.pending_commands() == []


# ── paper : limite traversante ──

def test_marketable_entry_fills_at_open_and_refunds():
    cfg = _cfg()
    br, j = PaperBroker(cfg, 1000), Journal()
    t = j.save(Trade("TEST-EUR", "x", 101, 90, 0, 1.0, entry_mode="marketable"))
    br.place_entry(t)
    assert br.available_quote() == pytest.approx(1000 - 101 * 1.005)
    assert br.poll_entry(t, _bar(100, 103, 99, 102))
    assert t.entry_price == pytest.approx(100 * 1.0005)
    assert t.entry_fee == pytest.approx(t.entry_price * 0.005)
    assert br.available_quote() == pytest.approx(1000 - t.entry_price - t.entry_fee)
    # pas d'objectif : un plus haut ne déclenche aucune sortie
    assert not br.poll_exit(t, _bar(102, 500, 101, 450))


# ── stratégie tendance ──

def _trend_frame(seed=1):
    # 250 j de plat (chauffe de la SMA200), 300 j de hausse bruitée, 200 j de baisse
    rng = np.random.default_rng(seed)
    path = np.concatenate([np.zeros(250), np.linspace(0, 1.2, 300), np.linspace(1.2, 0.5, 200)])
    n = len(path)
    close = 100 * np.exp(path + 0.03 * rng.standard_normal(n))
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1.0}, index=idx)


def test_trend_strategy_enters_trails_and_exits():
    cfg = _cfg(capital_eur=1000)
    strat = TrendStrategy(cfg)
    df = strat.features({"ONE_DAY": _trend_frame()})
    signals = [i for i in range(len(df)) if strat.signal_at(df, i, "TEST-EUR")]
    assert signals and min(signals) >= strat.warmup
    br, j = PaperBroker(cfg, 1000), Journal()
    eng = Engine(cfg, br, j, strat, capital=1000, risk_overrides=trend_risk_overrides(cfg))
    stops = {}
    for i in range(len(df)):
        eng.on_bar("TEST-EUR", df, i)
        for t in j.active():
            if t.status == "OPEN":
                assert t.stop >= stops.get(t.id, 0)        # le stop ne descend jamais
                stops[t.id] = t.stop
    closed = j.closed("trend")
    assert closed and all(t.sleeve == "trend" and not t.has_target for t in closed)
    assert {t.exit_reason for t in closed} <= {"stop", "trend_exit"}
    assert any(t.stop > t.initial_stop for t in closed)    # le stop a suivi


def test_trend_sizing_respects_risk_budget():
    cfg = _cfg(capital_eur=1000)
    rm = RiskManager(cfg, trend_risk_overrides(cfg))
    d = rm.approve(now=datetime(2026, 1, 1, tzinfo=UTC), entry=100, stop=90, equity=1000,
                   available_quote=5000, open_positions=0, trades_today=0, pnl_today=0,
                   start_of_day_equity=1000)
    assert d.ok and d.quote_size == pytest.approx(100)    # 1 % de 1000 / 10 % de stop


@pytest.mark.parametrize("controls,reason", [
    (Controls(paused=True), "pause"),
    (Controls(entries_blocked={"trend"}), "bloquées"),
    (Controls(risk_multiplier=0), "multiplicateur 0"),
    (Controls(blackouts=[(datetime(2025, 12, 31, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC),
                          "FOMC")]), "FOMC"),
])
def test_controls_block_entries(controls, reason):
    cfg = _cfg()
    d = RiskManager(cfg).approve(now=datetime(2026, 1, 1, tzinfo=UTC), entry=100, stop=98,
                                 equity=1000, available_quote=1000, open_positions=0,
                                 trades_today=0, pnl_today=0, start_of_day_equity=1000,
                                 controls=controls, sleeve="trend")
    assert not d.ok and reason in d.reason


def test_ai_multiplier_only_reduces_size():
    cfg = _cfg()
    kw = dict(now=datetime(2026, 1, 1, tzinfo=UTC), entry=100, stop=95, equity=10000,
              available_quote=10000, open_positions=0, trades_today=0, pnl_today=0,
              start_of_day_equity=10000)
    full = RiskManager(cfg).approve(**kw).quote_size
    half = RiskManager(cfg).approve(controls=Controls(risk_multiplier=0.5), **kw).quote_size
    more = RiskManager(cfg).approve(controls=Controls(risk_multiplier=3), **kw).quote_size
    assert half == pytest.approx(full / 2) and more == pytest.approx(full)


# ── DCA ──

def test_allocate_prefers_underweight_and_merges_small_orders():
    w = {"BTC-EUR": 0.7, "ETH-EUR": 0.3}
    assert allocate(23, w, {}, 10) == {"BTC-EUR": 23}
    assert allocate(23, w, {"BTC-EUR": 100, "ETH-EUR": 10}, 10) == {"ETH-EUR": 23}
    a = allocate(100, w, {}, 10)
    assert a == pytest.approx({"BTC-EUR": 70, "ETH-EUR": 30})
    assert allocate(5, w, {}, 10) == {}
    assert tilt_factor(80, 100) == 1.5 and tilt_factor(160, 100) == 0.75 and tilt_factor(110, 100) == 1


def test_dca_paper_flow_never_sells_and_survives_restart():
    cfg = _cfg()
    cfg.dca = dataclasses.replace(cfg.dca, monthly_eur=433.33, weights={"BTC-EUR": 0.7, "ETH-EUR": 0.3})
    br, j = PaperBroker(cfg, 1000), Journal()
    dca = DcaSleeve(cfg, br, j)
    t0 = datetime(2026, 10, 19, 9, tzinfo=UTC)
    lots = dca.buy({"BTC-EUR": 70000, "ETH-EUR": 2000}, now=t0)
    assert {l.product for l in lots} == {"BTC-EUR", "ETH-EUR"}
    assert sum(l.quote_amount for l in lots) == pytest.approx(100, rel=1e-3)
    assert dca.buy({"BTC-EUR": 70000, "ETH-EUR": 2000}, now=t0) == []     # pas de doublon

    def bars(product, since):        # BTC traverse la limite, ETH non
        px = 69000 if product == "BTC-EUR" else 2100
        return pd.DataFrame({"open": [px], "high": [px], "low": [px], "close": [px]},
                            index=[pd.Timestamp("2026-10-19T09:05Z")])

    dca.manage({"BTC-EUR": 70000, "ETH-EUR": 2100}, bars, now=t0 + timedelta(minutes=5))
    by = {l.product: l for l in j.lots()}
    assert by["BTC-EUR"].status == "FILLED" and by["ETH-EUR"].status == "PENDING"
    dca.manage({"BTC-EUR": 70000, "ETH-EUR": 2100}, bars, now=t0 + timedelta(minutes=31))
    assert j.lots("PENDING")[0].reprices == 1                 # reprix au nouveau bid
    dca.manage({"BTC-EUR": 70000, "ETH-EUR": 2100}, bars, now=t0 + timedelta(hours=7))
    eth = [l for l in j.lots() if l.product == "ETH-EUR"][0]
    assert eth.post_only == 0                                 # bascule en limite traversante
    dca.manage({"BTC-EUR": 70000, "ETH-EUR": 2100}, bars, now=t0 + timedelta(hours=7, minutes=5))
    assert all(l.status == "FILLED" for l in j.lots())
    assert all(q > 0 for q in br.base.values())
    fresh = PaperBroker(cfg)
    fresh.restore(j)
    prices = {"BTC-EUR": 70000, "ETH-EUR": 2100}
    assert fresh.equity(prices) == pytest.approx(br.equity(prices))


def test_dca_respects_live_cap():
    cfg = _cfg()
    br, j = PaperBroker(cfg, 1000), Journal()
    dca = DcaSleeve(cfg, br, j, deployable=lambda: 15.0)
    lots = dca.buy({"BTC-EUR": 70000, "ETH-EUR": 2000})
    assert sum(l.quote_amount for l in lots) <= 15.0
    assert DcaSleeve(cfg, br, Journal(), deployable=lambda: 5.0).buy({"BTC-EUR": 1, "ETH-EUR": 1}) == []


# ── promotion ──

def _promo_setup(monkeypatch, **promo):
    cfg = _cfg()
    cfg.promotion = dataclasses.replace(cfg.promotion, max_live_capital_eur=500, **promo)
    paper, control = Journal(), Journal()
    now = datetime(2026, 12, 1, tzinfo=UTC)
    for s in ("dca", "trend"):
        paper.set_state(f"paper_started:{s}", "2026-10-01T00:00:00Z")
    control.set_json("research", {"tendance": {"hors_echantillon": {
        "profit_factor": 1.3, "max_drawdown_pct": -12.0, "periode": "2023 → 2026"}}})
    monkeypatch.setenv("DAYBOT_LIVE_CONFIRM", "OUI")
    return cfg, paper, control, now


def test_promotion_requires_consent_days_and_oos(monkeypatch):
    cfg, paper, control, now = _promo_setup(monkeypatch)
    res = promotion.evaluate(cfg, paper, control, now, key_ok=True)
    assert res["dca"]["eligible"] and res["trend"]["eligible"]
    assert not promotion.evaluate(cfg, paper, control, now, key_ok=False)["trend"]["eligible"]
    monkeypatch.delenv("DAYBOT_LIVE_CONFIRM")
    assert not promotion.evaluate(cfg, paper, control, now, key_ok=True)["dca"]["eligible"]
    monkeypatch.setenv("DAYBOT_LIVE_CONFIRM", "OUI")
    early = promotion.evaluate(cfg, paper, control, datetime(2026, 10, 5, tzinfo=UTC), key_ok=True)
    assert not early["dca"]["eligible"]
    control.set_json("research", {"tendance": {"hors_echantillon": {"profit_factor": 1.1,
                                                                    "max_drawdown_pct": -5}}})
    res = promotion.evaluate(cfg, paper, control, now, key_ok=True)
    assert res["dca"]["eligible"] and not res["trend"]["eligible"]


def test_promotion_errors_demotion_and_auto_toggle(monkeypatch):
    cfg, paper, control, now = _promo_setup(monkeypatch)
    control.db.execute("INSERT INTO events (created, level, source, message) VALUES "
                       "('2026-11-28T00:00:00Z', 'ERROR', 'paper:trend', 'boom')")
    control.db.commit()
    assert not promotion.evaluate(cfg, paper, control, now, key_ok=True)["trend"]["eligible"]
    control.set_state("demoted:dca", "1")
    control.set_state("live_since:dca", "2026-11-01T00:00:00Z")
    r = promotion.evaluate(cfg, paper, control, now, key_ok=True)["dca"]
    assert not r["eligible"] and not r["live_allowed"]
    control.set_state("demoted:dca", "0")
    control.set_state("auto_promotion", "off")
    r = promotion.evaluate(cfg, paper, control, now, key_ok=True)["dca"]
    assert not r["eligible"] and r["live_allowed"]       # déjà promue : continue


def test_deployable_cap():
    cfg = _cfg()
    cfg.promotion = dataclasses.replace(cfg.promotion, max_live_capital_eur=100)
    live = Journal()
    live.save(Trade("BTC-EUR", "x", 50, 45, 0, 1.0, status="OPEN", entry_price=50, entry_fee=0.25))
    live.save_lot(DcaLot("BTC-EUR", "x", 20, 1, 20, status="FILLED", price=1, fee=0.05))
    assert promotion.deployable_eur(cfg, live) == pytest.approx(100 - 50.25 - 20.05)


# ── réconciliation live ──

class _Resp(dict):
    def to_dict(self):
        return dict(self)


class FakeClient:
    def __init__(self, open_orders, orders):
        self.open, self.orders, self.cancelled, self.created = open_orders, orders, [], []

    def list_orders(self, **kw):
        return _Resp(orders=self.open, has_next=False)

    def get_order(self, oid):
        return _Resp(order=self.orders[oid])

    def cancel_orders(self, order_ids):
        self.cancelled += order_ids
        return _Resp(results=[])

    def get_product(self, pid):
        return _Resp(base_increment="0.00000001", quote_increment="0.01", base_min_size="0.00001")

    def stop_limit_order_gtc_sell(self, **kw):
        self.created.append(kw)
        return _Resp(success=True, success_response={"order_id": f"new-{len(self.created)}"})


def _live(client):
    br = LiveBroker.__new__(LiveBroker)
    br.cfg, br.client, br._spec = _cfg(), client, {}
    return br


def test_reconcile_cancels_orphans_and_restores_protection(monkeypatch):
    monkeypatch.setattr("daybot.broker.time.sleep", lambda s: None)
    j = Journal()
    a = j.save(Trade("BTC-EUR", "x", 100, 90, 0, 0.5, status="OPEN", entry_price=100,
                     exit_order_id="stop-a", sleeve="trend"))
    b = j.save(Trade("ETH-EUR", "x", 10, 9, 0, 2.0, status="OPEN", entry_price=10,
                     exit_order_id="stop-b", sleeve="trend"))
    client = FakeClient(
        open_orders=[{"order_id": "orphan", "client_order_id": "daybot-zz", "product_id": "SOL-EUR"},
                     {"order_id": "manual", "client_order_id": "perso", "product_id": "BTC-EUR"}],
        orders={"stop-a": {"status": "FILLED", "average_filled_price": "89", "total_fees": "0.2",
                           "last_fill_time": "2026-10-20T01:00:00Z"},
                "stop-b": {"status": "CANCELLED"}})
    notes = _live(client).reconcile(j)
    assert client.cancelled == ["orphan"]                     # jamais l'ordre manuel
    assert j.trade(a.id).status == "CLOSED" and j.trade(a.id).exit_reason == "stop"
    assert j.trade(b.id).exit_order_id == "new-1"
    assert client.created[0]["stop_direction"] == "STOP_DIRECTION_STOP_DOWN"
    assert client.created[0]["client_order_id"].startswith("daybot-")
    assert len(notes) == 3
