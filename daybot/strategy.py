"""Stratégie long-only (spot) : suivi de tendance + rebond sur EMA, filtrée par les frais.

Règles, évaluées sur une bougie *clôturée* du timeframe signal :
  1. Régime : close du timeframe trend > EMA(trend) et EMA(trend) montante.
     (spot = pas de vente à découvert ; en régime baissier le bot reste en EUR.)
  2. Déclencheur : croisement EMA rapide > EMA lente, OU rebond (mèche sous
     l'EMA lente, clôture au-dessus) alors que rapide > lente.
  3. Momentum : RSI dans [rsi_min, rsi_max] (ni faible, ni en surachat).
  4. Edge : (TP - entrée) / entrée ≥ min_edge_over_fees × frais aller-retour.
     Sans cette règle, à 0,25 %/0,50 % de frais, la plupart des micro-mouvements
     intraday sont perdants d'avance.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import Config
from .indicators import atr, ema, rsi
from .market import SECONDS


@dataclass
class Signal:
    product: str
    time: pd.Timestamp
    entry: float      # prix limite post-only
    stop: float
    target: float
    atr: float


def features(sig: pd.DataFrame, trend: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    s = cfg.strategy
    df = sig.copy()
    df["ema_f"] = ema(df["close"], s.ema_fast)
    df["ema_s"] = ema(df["close"], s.ema_slow)
    df["rsi"] = rsi(df["close"], s.rsi_period)
    df["atr"] = atr(df, s.atr_period)

    t = trend[["close"]].copy()
    t["ema_t"] = ema(t["close"], s.ema_trend)
    t["trend_up"] = (t["close"] > t["ema_t"]) & (t["ema_t"] > t["ema_t"].shift(3))
    # Une bougie trend horodatée H n'est connue qu'à H + durée : on la décale
    # pour éviter tout biais de look-ahead.
    t.index = t.index + pd.Timedelta(seconds=SECONDS[cfg.trend_tf])
    # Idem pour le signal : une bougie est exploitable à sa clôture.
    df["known_at"] = df.index + pd.Timedelta(seconds=SECONDS[cfg.signal_tf])
    merged = pd.merge_asof(df.sort_values("known_at"), t[["trend_up"]],
                           left_on="known_at", right_index=True, direction="backward")
    merged.index = df.index
    df["trend_up"] = merged["trend_up"].fillna(False).astype(bool)
    return df


def signal_at(df: pd.DataFrame, i: int, product: str, cfg: Config) -> Signal | None:
    s, f = cfg.strategy, cfg.fees
    warmup = max(s.ema_slow, s.atr_period, s.rsi_period) * 3
    if i < warmup:
        return None
    row, prev = df.iloc[i], df.iloc[i - 1]
    if not row["trend_up"]:
        return None
    cross = prev["ema_f"] <= prev["ema_s"] and row["ema_f"] > row["ema_s"]
    bounce = (row["ema_f"] > row["ema_s"] and row["low"] <= row["ema_s"] < row["close"])
    if not (cross or bounce):
        return None
    if not (s.rsi_min <= row["rsi"] <= s.rsi_max):
        return None

    a = row["atr"]
    entry = row["close"] - s.entry_offset_atr * a
    stop = entry - s.stop_atr * a
    target = entry + s.reward_risk * (entry - stop)
    round_trip = f.maker + f.taker
    if (target - entry) / entry < s.min_edge_over_fees * round_trip:
        return None
    return Signal(product, df.index[i], entry, stop, target, a)
