"""Backtest sur l'historique public Coinbase, avec frais réels et slippage.

Le backtest passe par le même Engine et le même PaperBroker que le paper et le live :
ce qui est testé est exactement ce qui tourne.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd

from . import market
from .broker import PaperBroker
from .config import Config
from .engine import Engine
from .journal import Journal
from .strategies.trend import TrendStrategy, trend_risk_overrides
from .strategy import features

# Découpage hors échantillon (paramètres fixés à l'avance, donc « IS » = simple référence).
IS_START, OOS_START = "2018-01-01", "2023-01-01"


def load_data(cfg: Config, days: int, progress=None) -> dict[str, pd.DataFrame]:
    out = {}
    for k, p in enumerate(cfg.products, 1):
        n_sig = days * 86400 // market.SECONDS[cfg.signal_tf]
        n_tr = days * 86400 // market.SECONDS[cfg.trend_tf] + cfg.strategy.ema_trend * 3
        if progress:
            progress(f"[{k}/{len(cfg.products)}] Téléchargement {p} : {n_sig} bougies {cfg.signal_tf} "
                     f"+ {n_tr} bougies {cfg.trend_tf}…")
        sig = market.candles(p, cfg.signal_tf, n_sig)
        tr = market.candles(p, cfg.trend_tf, n_tr)
        out[p] = features(sig, tr, cfg)
    return out


def run(cfg: Config, data: dict[str, pd.DataFrame], no_fees: bool = False, progress=None,
        strategy=None, capital: float | None = None, risk_overrides: dict | None = None,
        start: str | None = None, end: str | None = None) -> dict:
    """`data` = features par paire. `start`/`end` limitent la période simulée ; les
    bougies antérieures servent seulement au calcul des indicateurs."""
    # Les trades simulés ne doivent pas apparaître dans les logs du bot comme de vrais trades.
    logger = logging.getLogger("daybot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        return _run(cfg, data, no_fees, progress, strategy, capital, risk_overrides, start, end)
    finally:
        logger.setLevel(previous)


def _run(cfg, data, no_fees, progress, strategy, capital, risk_overrides, start, end) -> dict:
    # Le backtest ignore kill switch / blackout réels.
    cfg = dataclasses.replace(cfg, notifications=False, risk=dataclasses.replace(
        cfg.risk, kill_switch_file="/nonexistent", blackout=[]))
    if capital is not None:
        cfg = dataclasses.replace(cfg, paper_balance=capital)
    # no_fees : mêmes signaux (le filtre d'edge garde les frais), exécution sans frais.
    broker_cfg = dataclasses.replace(cfg, fees=dataclasses.replace(
        cfg.fees, maker=0, taker=0, slippage=0)) if no_fees else cfg
    broker = PaperBroker(broker_cfg)
    journal = Journal(":memory:")
    if strategy is not None:
        strategy = type(strategy)(cfg)              # même stratégie, cfg du backtest
    eng = Engine(cfg, broker, journal, strategy, capital=capital, risk_overrides=risk_overrides)
    lo = pd.Timestamp(start, tz="UTC") if start else None
    hi = pd.Timestamp(end, tz="UTC") if end else None
    events = sorted((ts, p, i) for p, df in data.items() for i, ts in enumerate(df.index)
                    if (lo is None or ts >= lo) and (hi is None or ts <= hi))
    curve = []
    step = max(len(events) // 4, 1)
    for n, (ts, p, i) in enumerate(events):
        if progress and n and n % step == 0:
            progress(f"  simulation {'sans' if no_fees else 'avec'} frais : {100 * n // len(events)} %")
        eng.on_bar(p, data[p], i)
        curve.append((ts, broker.equity(eng.prices)))
    for t in journal.active():       # clôture de fin de test
        last = data[t.product]
        last = last[last.index <= hi] if hi is not None else last
        if t.status == "OPEN":
            broker.force_exit(t, last["close"].iloc[-1], last.index[-1], "end")
            t.status = "CLOSED"
        else:
            broker.cancel_entry(t)
            t.status = "CANCELLED"
        journal.save(t)
    if curve:
        curve.append((curve[-1][0], broker.equity(eng.prices)))
    return summarize(cfg, journal, broker, curve, data, lo, hi)


def _yearly(eq: pd.Series, start_value: float) -> dict[str, float]:
    if eq.empty:
        return {}
    ends = eq.groupby(eq.index.year).last()
    prev, out = start_value, {}
    for year, v in ends.items():
        out[str(year)] = round((v / prev - 1) * 100, 2) if prev else 0.0
        prev = v
    return out


def summarize(cfg, journal, broker, curve, data, lo=None, hi=None) -> dict:
    trades = journal.closed()
    eq = pd.Series([e for _, e in curve], index=[t for t, _ in curve])
    eq = eq[~eq.index.duplicated(keep="last")]
    dd = (eq / eq.cummax() - 1).min() if len(eq) else 0.0
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    by_reason = defaultdict(int)
    by_product = defaultdict(float)
    for t in trades:
        by_reason[t.exit_reason] += 1
        by_product[t.product] += t.pnl
    bh = {}
    for p, df in data.items():
        if lo is None and hi is None:
            win = df.iloc[cfg.strategy.ema_slow * 3:]
        else:
            win = df
            if lo is not None:
                win = win[win.index >= lo]
            if hi is not None:
                win = win[win.index <= hi]
        if len(win) > 1:
            bh[p] = win["close"].iloc[-1] / win["close"].iloc[0] - 1
    start = cfg.paper_balance
    end = eq.iloc[-1] if len(eq) else start
    first = eq.index[0] if len(eq) else min(df.index[0] for df in data.values())
    last = eq.index[-1] if len(eq) else max(df.index[-1] for df in data.values())
    years = max((last - first).days / 365.25, 1e-9)
    cagr = (end / start) ** (1 / years) - 1 if start and end > 0 and years >= 0.5 else None
    return {
        "periode": f"{first:%Y-%m-%d} → {last:%Y-%m-%d}",
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
        "rendement_annualise_pct": round(cagr * 100, 2) if cagr is not None else None,
        "par_annee_pct": _yearly(eq, start),
    }


# ── Recherche : tendance multi-années + DCA ──

def _ts(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def load_history(cfg: Config, strategy, products: list[str], since: str,
                 progress=None) -> dict[str, pd.DataFrame]:
    """Features par paire depuis `since` (moins la période de chauffe), via le cache."""
    out = {}
    for k, p in enumerate(products, 1):
        if progress:
            progress(f"[{k}/{len(products)}] Historique {p} depuis {since}…")
        frames = {}
        for tf in strategy.frames():
            warm = (strategy.warmup + 5) * market.SECONDS[tf]
            frames[tf] = market.history(p, tf, _ts(since) - warm,
                                        cache_path=cfg.market_cache_path)
        if all(len(f) for f in frames.values()):
            out[p] = strategy.features(frames)
    return out


def trend_research(cfg: Config, progress=None) -> dict:
    strat = TrendStrategy(cfg)
    data = load_history(cfg, strat, cfg.trend.products, IS_START, progress)
    kw = dict(strategy=strat, capital=cfg.trend.capital_eur,
              risk_overrides=trend_risk_overrides(cfg))
    full = run(cfg, data, start=IS_START, **kw)
    ins = run(cfg, data, start=IS_START, end=OOS_START, **kw)
    oos = run(cfg, data, start=OOS_START, **kw)
    oos_no_fees = run(cfg, data, start=OOS_START, no_fees=True, **kw)
    oos["rendement_sans_frais_pct"] = oos_no_fees["rendement_pct"]
    return {"complet": full, "dans_echantillon": ins, "hors_echantillon": oos}


def dca_backtest(daily: dict[str, pd.DataFrame], weights: dict[str, float], weekly_eur: float,
                 fee: float, tilt: bool = False, start: str = "2019-01-01",
                 weekday: int = 0) -> dict:
    """Achat chaque semaine (clôture du jour `weekday`) réparti selon `weights`, frais maker.
    tilt : ×1,5 sous la SMA200, ×0,75 au-dessus de 1,5×SMA200."""
    lo = pd.Timestamp(start, tz="UTC")
    qty = defaultdict(float)
    invested = 0.0
    curve = []
    common = None
    for p in weights:
        idx = daily[p].index[daily[p].index >= lo]
        common = idx if common is None else common.intersection(idx)
    if common is None or common.empty:
        return {}
    sma = {p: daily[p]["close"].rolling(200).mean() for p in weights}
    for ts in common:
        prices = {p: daily[p].at[ts, "close"] for p in weights}
        if ts.weekday() == weekday:
            for p, w in weights.items():
                amount = weekly_eur * w
                if tilt and not math.isnan(sma[p].at[ts]):
                    if prices[p] < sma[p].at[ts]:
                        amount *= 1.5
                    elif prices[p] > 1.5 * sma[p].at[ts]:
                        amount *= 0.75
                qty[p] += amount * (1 - fee) / prices[p]
                invested += amount
        value = sum(qty[p] * prices[p] for p in weights)
        curve.append((ts, value, invested))
    ts, value, invested = curve[-1]
    gains = pd.Series([v - i for _, v, i in curve])
    return {
        "periode": f"{common[0]:%Y-%m-%d} → {common[-1]:%Y-%m-%d}",
        "investi": round(invested, 2),
        "valeur": round(value, 2),
        "gain_pct": round((value / invested - 1) * 100, 2) if invested else 0.0,
        "pire_moins_value_eur": round(float(gains.min()), 2),
        "prix_moyen": {p: round(invested * weights[p] / qty[p], 2) if qty[p] else None
                       for p in weights},
    }


def dca_research(cfg: Config, progress=None) -> dict:
    daily = {}
    for p in cfg.dca.weights:
        if progress:
            progress(f"Historique journalier {p} (DCA)…")
        daily[p] = market.history(p, "ONE_DAY", _ts("2018-01-01"), cache_path=cfg.market_cache_path)
    weekly = cfg.dca.monthly_eur * 12 / 52
    plain = dca_backtest(daily, cfg.dca.weights, weekly, cfg.fees.maker, weekday=cfg.dca.weekday)
    tilted = dca_backtest(daily, cfg.dca.weights, weekly, cfg.fees.maker, tilt=True,
                          weekday=cfg.dca.weekday)
    return {"dca": plain, "dca_tilt": tilted}


def research(cfg: Config, progress=None) -> dict:
    out = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out["tendance"] = trend_research(cfg, progress)
    out.update(dca_research(cfg, progress))
    return out
