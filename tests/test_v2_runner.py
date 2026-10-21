"""Planificateur et processus permanent (marché simulé, aucun appel réseau)."""
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from daybot import market
from daybot.broker import PaperBroker
from daybot.config import Config
from daybot.journal import Journal
from daybot.runner import Runner
from daybot.scheduler import Daily, Every, Scheduler, Weekly

UTC = timezone.utc
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


# ── planificateur ──

def test_schedules_next_after_with_dst():
    assert Every(60).next_after(datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)) == datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    assert Every(900, offset=10).next_after(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)) == \
        datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    paris = Daily(7, 30, "Europe/Paris")
    assert paris.next_after(datetime(2026, 7, 1, 6, 0, tzinfo=UTC)) == datetime(2026, 7, 2, 5, 30, tzinfo=UTC)
    assert paris.next_after(datetime(2026, 12, 1, 6, 0, tzinfo=UTC)) == datetime(2026, 12, 1, 6, 30, tzinfo=UTC)
    # passage à l'heure d'hiver le 25/10/2026 : 7 h 30 Paris = 6 h 30 UTC
    assert paris.next_after(datetime(2026, 10, 24, 6, 0, tzinfo=UTC)) == datetime(2026, 10, 25, 6, 30, tzinfo=UTC)
    monday = Weekly(0, 9, 0, "Europe/Paris")
    nxt = monday.next_after(NOW)                                    # mercredi → lundi suivant
    assert nxt.astimezone(monday.tz).weekday() == 0 and nxt == datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


def test_scheduler_persists_and_isolates_failures():
    store = Journal()
    calls, errors = [], []
    s = Scheduler(store, on_error=lambda n, e: errors.append(n))
    s.add("a", Every(60), lambda: calls.append("a"), run_at_start=True)
    s.add("boom", Every(60), lambda: 1 / 0, run_at_start=True)
    s.add("later", Every(3600), lambda: calls.append("later"))
    assert s.tick(NOW) == ["a", "boom"] and errors == ["boom"]
    assert s.tick(NOW + timedelta(seconds=30)) == []
    s2 = Scheduler(store)                                          # redémarrage
    s2.add("a", Every(60), lambda: calls.append("a2"), run_at_start=True)
    assert s2.tick(NOW + timedelta(seconds=30)) == []              # pas de double exécution
    assert s2.tick(NOW + timedelta(seconds=61)) == ["a"]
    assert store.get_state("job:a:last_ok")


# ── processus permanent ──

def _series(n=700, seed=3):
    rng = np.random.default_rng(seed)
    path = np.concatenate([np.zeros(250), np.linspace(0, 0.8, n - 250)])
    close = 100 * np.exp(path + 0.02 * rng.standard_normal(n))
    idx = pd.date_range(end=NOW.replace(hour=0) - timedelta(days=1), periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1.0}, index=idx)


class FakeLive(PaperBroker):
    def reconcile(self, journal):
        return []

    def refresh_fees(self):
        return {"pricing_tier": "Intro"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    daily = _series()
    monkeypatch.setattr(market, "history", lambda p, gran, start, end=None, cache_path=None, now=None:
                        daily[daily.index >= pd.Timestamp(start, unit="s", tz="UTC")] if gran == "ONE_DAY"
                        else daily.iloc[-3:])
    monkeypatch.setattr(market, "last_price", lambda p: float(daily["close"].iloc[-1]))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DAYBOT_LIVE_CONFIRM", raising=False)
    cfg = Config(data_dir=str(tmp_path), journal_path=str(tmp_path / "journal.sqlite"))
    cfg.risk = dataclasses.replace(cfg.risk, kill_switch_file=str(tmp_path / "STOP"))
    cfg.trend = dataclasses.replace(cfg.trend, products=["BTC-EUR", "ETH-EUR"])
    Journal(cfg.control_path).set_json("research", {"tendance": {"hors_echantillon": {
        "profit_factor": 1.3, "max_drawdown_pct": -10, "rendement_pct": 5}}})
    return cfg


def test_runner_start_heartbeat_bars_and_snapshot(env):
    r = Runner(env, now=lambda: NOW)
    assert r.analyst is None and set(r.engines["paper"]) == {"trend"}
    ran = r.sched.tick(NOW)
    assert {"heartbeat", "prices", "promotion", "bars:trend", "equity"} <= set(ran)
    assert Path(env.path("heartbeat")).exists()
    assert r.paper_j.get_state("last_bar:trend:BTC-EUR")
    # une même bougie n'est jamais traitée deux fois
    held = {t.id: t.bars_held for t in r.paper_j.active()}
    r.process_bars("trend")
    assert {t.id: t.bars_held for t in r.paper_j.active()} == held
    assert [s for s in ("total", "trend", "dca") if r.paper_j.equity_curve(s)] == ["total", "trend", "dca"]
    assert r.live is None                                          # pas d'accord → pas de réel


def test_runner_processes_web_commands(env):
    r = Runner(env, now=lambda: NOW)
    c = Journal(env.control_path)
    for kind, payload in [("pause", {}), ("kill_on", {}), ("demote", {"sleeve": "trend"}),
                          ("ask", {"question": "?"}), ("bogus", {})]:
        c.add_command(kind, payload, "moi@example.com")
    r.process_commands()
    rows = {row["kind"]: row for row in c.commands()}
    assert rows["pause"]["status"] == "DONE" and r.controls("paper").paused
    assert Path(env.risk.kill_switch_file).exists()
    assert c.get_state("demoted:trend") == "1"
    assert rows["ask"]["status"] == "ERROR" and "ANTHROPIC" in rows["ask"]["result"]
    assert rows["bogus"]["status"] == "ERROR"
    assert c.events(level="INFO")                                 # journalisé pour le site


def test_runner_promotes_to_live_with_cap(env, monkeypatch):
    monkeypatch.setenv("DAYBOT_LIVE_CONFIRM", "OUI")
    monkeypatch.setattr("daybot.promotion.has_live_key", lambda: True)
    env.promotion = dataclasses.replace(env.promotion, max_live_capital_eur=50)
    r = Runner(env, live_broker_factory=lambda cfg: FakeLive(cfg, 1000), now=lambda: NOW)
    r.update_promotion()
    assert r.live is None                                          # 0 jour de paper
    for s in ("dca", "trend"):
        r.paper_j.set_state(f"paper_started:{s}", "2026-08-01T00:00:00Z")
    r.update_promotion()
    assert r.live is not None and set(r.engines["live"]) == {"trend"}
    assert r.live_allowed("dca") and r.live_allowed("trend")
    assert "promue" in Journal(env.control_path).events(level="WARNING")[0]["message"]
    r.refresh_prices()
    r.dca_buy()
    live_lots = r.live_j.lots()
    assert live_lots and sum(lot.quote_amount for lot in live_lots) <= 50   # plafond d'euros réels
    # rétrogradation : la poche DCA réelle n'achète plus
    r.execute("demote", {"sleeve": "dca"})
    assert not r.live_allowed("dca")
    assert r.dca["live"].buy(r.prices) == []
    assert r.controls("live").entries_blocked == set()            # tendance toujours autorisée
    r.execute("demote", {"sleeve": "trend"})
    assert r.controls("live").entries_blocked == {"trend"}


def test_runner_backup_keeps_14_days(env):
    r = Runner(env, now=lambda: NOW)
    for d in range(20):
        (Path(env.path("backups")) / f"2026-09-{d + 1:02d}").mkdir(parents=True)
    r.backup()
    days = sorted(p.name for p in Path(env.path("backups")).iterdir())
    assert len(days) == 14 and days[-1] == "2026-10-21"
    assert (Path(env.path("backups")) / "2026-10-21" / "control.sqlite").exists()
