import dataclasses
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from daybot import backtest
from daybot.broker import PaperBroker
from daybot.config import Config, load
from daybot.journal import Journal, Trade
from daybot.risk import RiskManager


def _cfg(**risk):
    cfg = Config(products=["TEST-EUR"])
    risk = {"kill_switch_file": "/nonexistent", **risk}
    return dataclasses.replace(cfg, risk=dataclasses.replace(cfg.risk, **risk))


def _approve(rm, **kw):
    base = dict(now=datetime(2026, 10, 20, tzinfo=timezone.utc), entry=100.0, stop=98.0,
                equity=1000.0, available_quote=1000.0, open_positions=0, trades_today=0,
                pnl_today=0.0, start_of_day_equity=1000.0)
    return rm.approve(**{**base, **kw})


def test_config_file_loads():
    cfg = load("config.toml")
    assert cfg.fees.maker == 0.0025 and cfg.fees.taker == 0.005
    assert cfg.risk.blackout[0][0].year == 2026


def test_sizing_risks_fixed_fraction():
    d = _approve(RiskManager(_cfg()))
    # 0,5 % de 1000 = 5 € de risque ; stop à 2 % → 250 €, plafonné à 25 % = 250 €
    assert d.ok and d.quote_size == pytest.approx(250.0)
    d = _approve(RiskManager(_cfg()), stop=95.0)       # stop 5 % → 100 €
    assert d.quote_size == pytest.approx(100.0)


@pytest.mark.parametrize("kw,reason", [
    (dict(open_positions=2), "positions"),
    (dict(trades_today=6), "trades"),
    (dict(pnl_today=-25.0), "perte"),
    (dict(equity=30.0, available_quote=30.0), "minimum"),
])
def test_guards(kw, reason):
    d = _approve(RiskManager(_cfg()), **kw)
    assert not d.ok and reason in d.reason


def test_blackout_and_drawdown():
    t0 = datetime(2026, 10, 28, 17, tzinfo=timezone.utc)
    rm = RiskManager(_cfg(blackout=[(t0.replace(hour=16), t0.replace(hour=20))]))
    assert "blackout" in _approve(rm, now=t0).reason
    rm = RiskManager(_cfg())
    rm.update_equity(1000); rm.update_equity(890)
    assert not _approve(rm).ok


def test_kill_switch(tmp_path):
    f = tmp_path / "STOP"; f.touch()
    assert "kill" in _approve(RiskManager(_cfg(kill_switch_file=str(f)))).reason


def _bar(o, h, l, c, ts="2026-10-20T10:00Z"):
    return pd.Series({"open": o, "high": h, "low": l, "close": c}, name=pd.Timestamp(ts))


def test_paper_broker_fees_and_conservative_fills():
    cfg = _cfg()
    br, j = PaperBroker(cfg, 1000), Journal()
    t = j.save(Trade("TEST-EUR", "x", 100, 98, 104, 1.0))
    br.place_entry(t)
    assert not br.poll_entry(t, _bar(101, 102, 100, 101))      # touche sans traverser
    assert br.poll_entry(t, _bar(101, 102, 99.9, 101))
    assert t.entry_fee == pytest.approx(0.25)
    # stop et TP dans la même bougie → stop
    assert br.poll_exit(t, _bar(100, 105, 97, 100))
    assert t.exit_reason == "stop" and t.exit_fee == pytest.approx(98 * 0.9995 * 0.005)
    t.status = "CLOSED"
    assert br.equity({}) == pytest.approx(1000 + t.pnl)


def test_journal_roundtrip_and_tax_export(tmp_path):
    j = Journal(str(tmp_path / "j.sqlite"))
    t = j.save(Trade("BTC-EUR", "x", 100, 98, 104, 0.5, status="CLOSED", entry_price=100,
                     exit_price=104, entry_fee=0.1, exit_fee=0.1, entry_time="2026-10-20T10:00:00Z",
                     exit_time="2026-10-20T12:00:00Z", exit_reason="take_profit"))
    assert Journal(str(tmp_path / "j.sqlite")).closed()[0].pnl == pytest.approx(1.8)
    assert j.day_stats("2026-10-20") == (1, pytest.approx(1.8))
    out = tmp_path / "c.csv"
    assert j.export_tax_csv(str(out)) == 1
    assert "BTC;0.50000000" in out.read_text()


def test_backtest_runs_on_synthetic_trend():
    rng = np.random.default_rng(0)
    n = 3000
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = 100 * np.exp(np.cumsum(0.0004 + 0.006 * rng.standard_normal(n)))
    sig = pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996,
                        "close": close, "volume": 1.0}, index=idx)
    trend = sig.resample("1h").agg({"open": "first", "high": "max", "low": "min",
                                    "close": "last", "volume": "sum"})
    cfg = _cfg()
    from daybot.strategy import features
    res = backtest.run(cfg, {"TEST-EUR": features(sig, trend, cfg)})
    assert res["trades"] > 0
    assert res["capital_final"] > 0


# ── Portabilité macOS ──

def test_paths_resolved_from_config_dir(tmp_path, monkeypatch):
    cfg_file = tmp_path / "bot" / "config.toml"
    cfg_file.parent.mkdir()
    cfg_file.write_text('mode = "paper"\n[risk]\nkill_switch_file = "data/STOP"\n')
    monkeypatch.chdir(tmp_path)                    # dossier courant ≠ dossier du bot
    cfg = load(cfg_file)
    assert cfg.journal_path == str(cfg_file.parent / "data" / "journal.sqlite")
    assert cfg.risk.kill_switch_file == str(cfg_file.parent / "data" / "STOP")
    assert cfg.data_dir == str(cfg_file.parent / "data")


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        del self.store[(service, name)]


def test_credentials_env_then_keychain(tmp_path, monkeypatch):
    from daybot import credentials
    kr = _FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: kr)
    for n in ("COINBASE_KEY_FILE", "COINBASE_API_KEY", "COINBASE_API_SECRET"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(RuntimeError, match="keychain-import"):
        credentials.credentials()

    pem = "-----BEGIN EC PRIVATE KEY-----\nABC\n-----END EC PRIVATE KEY-----\n"
    key = tmp_path / "cdp_api_key.json"
    key.write_text('{"name": "organizations/o/apiKeys/k1", "privateKey": %s}' % __import__("json").dumps(pem))
    assert credentials.import_key_file(str(key)).endswith("k1")
    assert credentials.credentials() == {"api_key": "organizations/o/apiKeys/k1", "api_secret": pem}

    monkeypatch.setenv("COINBASE_API_KEY", "env-key")          # l'environnement prime
    assert credentials.credentials()["api_key"] == "env-key"
    monkeypatch.setenv("COINBASE_KEY_FILE", "~/k.json")
    assert credentials.credentials() == {"key_file": str(__import__("pathlib").Path("~/k.json").expanduser())}

    credentials.forget()
    assert kr.store == {}


def test_notify_uses_osascript_argv_on_macos(monkeypatch):
    from daybot import notify as nmod
    calls = []
    monkeypatch.setattr(nmod.sys, "platform", "darwin")
    monkeypatch.setattr(nmod.shutil, "which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr(nmod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert nmod.notify('BTC-EUR "x"', "P&L +1.00")
    cmd = calls[0]
    assert cmd[0] == "osascript" and cmd[-2:] == ['BTC-EUR "x"', "P&L +1.00"]
    assert not any('"x"' in part for part in cmd[:-2])            # jamais dans le script
    assert not nmod.notify("t", "m", enabled=False)
    monkeypatch.setattr(nmod.sys, "platform", "linux")
    assert not nmod.notify("t", "m")


def test_paper_restore_after_restart():
    cfg = _cfg()
    j = Journal()
    br = PaperBroker(cfg, 1000)
    closed = j.save(Trade("TEST-EUR", "x", 100, 98, 104, 1.0))
    br.place_entry(closed); br.poll_entry(closed, _bar(101, 102, 99, 101))
    br.poll_exit(closed, _bar(101, 105, 100.5, 104, "2026-10-20T11:00Z")); closed.status = "CLOSED"
    j.save(closed)
    opened = j.save(Trade("TEST-EUR", "y", 100, 98, 104, 2.0))
    br.place_entry(opened); br.poll_entry(opened, _bar(101, 102, 99, 101)); opened.status = "OPEN"
    j.save(opened)
    pending = j.save(Trade("TEST-EUR", "z", 90, 88, 94, 1.0))
    br.place_entry(pending); j.save(pending)

    fresh = PaperBroker(cfg)
    fresh.restore(j)
    prices = {"TEST-EUR": 101.0}
    assert fresh.available_quote() == pytest.approx(br.available_quote())
    assert fresh.equity(prices) == pytest.approx(br.equity(prices))
    assert fresh.total_fees == pytest.approx(br.total_fees)


def test_engine_state_survives_restart():
    from daybot.engine import Engine
    cfg = _cfg()
    j = Journal()
    j.set_state("peak_equity", 1200); j.set_state("day", "2026-10-20")
    j.set_state("day_start_equity", 1100)
    eng = Engine(cfg, PaperBroker(cfg, 1000), j)
    assert eng.risk.peak_equity == 1200 and eng.day_start_equity == 1100
    eng.risk.update_equity(1000)       # −16,7 % depuis le plus haut conservé → arrêt
    assert eng.risk.halted_reason
