"""Backtest sur l'historique public Coinbase, avec frais réels et slippage."""
from __future__ import annotations

import dataclasses
from collections import defaultdict

import pandas as pd

from . import market
from .broker import PaperBroker
from .config import Config
from .engine import Engine
from .journal import Journal
from .strategy import features


def load_data(cfg: Config, days: int) -> dict[str, pd.DataFrame]:
    out = {}
    for p in cfg.products:
        n_sig = days * 86400 // market.SECONDS[cfg.signal_tf]
        n_tr = days * 86400 // market.SECONDS[cfg.trend_tf] + cfg.strategy.ema_trend * 3
        sig = market.candles(p, cfg.signal_tf, n_sig)
        tr = market.candles(p, cfg.trend_tf, n_tr)
        out[p] = features(sig, tr, cfg)
    return out


def run(cfg: Config, data: dict[str, pd.DataFrame], no_fees: bool = False) -> dict:
    # Le backtest ignore kill switch / blackout réels.
    cfg = dataclasses.replace(cfg, notifications=False, risk=dataclasses.replace(
        cfg.risk, kill_switch_file="/nonexistent", blackout=[]))
    # no_fees : mêmes signaux (le filtre d'edge garde les frais), exécution sans frais.
    broker_cfg = dataclasses.replace(cfg, fees=dataclasses.replace(
        cfg.fees, maker=0, taker=0, slippage=0)) if no_fees else cfg
    broker = PaperBroker(broker_cfg)
    journal = Journal(":memory:")
    eng = Engine(cfg, broker, journal)
    events = sorted((ts, p, i) for p, df in data.items() for i, ts in enumerate(df.index))
    curve = []
    for ts, p, i in events:
        eng.on_bar(p, data[p], i)
        curve.append((ts, broker.equity(eng.prices)))
    for t in journal.active():       # clôture de fin de test
        if t.status == "OPEN":
            last = data[t.product]
            broker.force_exit(t, last["close"].iloc[-1], last.index[-1], "end")
            t.status = "CLOSED"
        else:
            broker.cancel_entry(t)
            t.status = "CANCELLED"
        journal.save(t)
    return summarize(cfg, journal, broker, curve, data)


def summarize(cfg, journal, broker, curve, data) -> dict:
    trades = journal.closed()
    eq = pd.Series([e for _, e in curve], index=[t for t, _ in curve])
    dd = (eq / eq.cummax() - 1).min() if len(eq) else 0.0
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    by_reason = defaultdict(int)
    by_product = defaultdict(float)
    for t in trades:
        by_reason[t.exit_reason] += 1
        by_product[t.product] += t.pnl
    bh = {p: df["close"].iloc[-1] / df["close"].iloc[cfg.strategy.ema_slow * 3] - 1
          for p, df in data.items()}
    start = cfg.paper_balance
    end = eq.iloc[-1] if len(eq) else start
    return {
        "periode": f"{min(df.index[0] for df in data.values()):%Y-%m-%d} → "
                   f"{max(df.index[-1] for df in data.values()):%Y-%m-%d}",
        "capital_initial": round(start, 2),
        "capital_final": round(end, 2),
        "rendement_pct": round((end / start - 1) * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "trades": len(trades),
        "taux_reussite_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) else None,
        "frais_payes": round(broker.total_fees, 2),
        "sorties": dict(by_reason),
        "pnl_par_paire": {k: round(v, 2) for k, v in by_product.items()},
        "buy_and_hold_pct": {k: round(v * 100, 2) for k, v in bh.items()},
    }
