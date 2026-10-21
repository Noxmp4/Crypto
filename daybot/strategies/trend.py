"""Suivi de tendance journalier, long-only (spot).

Règles classiques (cassure de Donchian « à la Turtle »), paramètres de manuel
fixés à l'avance — volontairement NON optimisés sur l'historique :
  - Régime   : clôture > SMA(200) — on ne cherche des achats qu'en tendance de fond haussière.
  - Entrée   : clôture > plus haut des 20 bougies précédentes.
  - Stop     : chandelier = plus haut depuis l'entrée − 3 × ATR(20), relevé chaque jour,
               jamais abaissé, posé sur l'exchange.
  - Sortie   : clôture < plus bas des 10 bougies précédentes (vente au marché), ou stop.
  - Taille   : 1 % du capital tactique risqué au stop initial (géré par RiskManager).
Peu de trades par an et des mouvements visés bien plus grands que les frais (≈ 0,75 %).
"""
from __future__ import annotations

import pandas as pd

from ..config import Config
from ..indicators import atr
from ..journal import Trade
from ..market import SECONDS
from ..strategy import Signal
from .base import Strategy


class TrendStrategy(Strategy):
    name = "trend"
    entry_mode = "marketable"     # une cassure se prend tout de suite, quitte à payer taker
    entry_ttl_bars = 1

    def __init__(self, cfg: Config):
        self.cfg = cfg
        t = cfg.trend
        self.p = t
        self.signal_tf = t.timeframe
        self.warmup = max(t.sma_regime, t.donchian_entry, t.atr_period * 3) + 1

    def frames(self) -> dict[str, int]:
        return {self.signal_tf: self.warmup + 60}

    def features(self, frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
        t = self.p
        df = frames[self.signal_tf].copy()
        df["sma_regime"] = df["close"].rolling(t.sma_regime).mean()
        # Canaux calculés sur les bougies PRÉCÉDENTES (shift) : la bougie courante
        # ne peut pas se « casser » elle-même.
        df["dc_high"] = df["high"].rolling(t.donchian_entry).max().shift(1)
        df["dc_low"] = df["low"].rolling(t.donchian_exit).min().shift(1)
        df["atr"] = atr(df, t.atr_period)
        return df

    def signal_at(self, df: pd.DataFrame, i: int, product: str) -> Signal | None:
        if i < self.warmup:
            return None
        row = df.iloc[i]
        if pd.isna(row["sma_regime"]) or pd.isna(row["dc_high"]):
            return None
        if not (row["close"] > row["sma_regime"] and row["close"] > row["dc_high"]):
            return None
        entry = row["close"] * (1 + self.p.entry_buffer)
        stop = row["close"] - self.p.atr_mult * row["atr"]
        if stop <= 0 or stop >= entry:
            return None
        return Signal(product, df.index[i], entry, stop, 0.0, row["atr"])

    def exit_at(self, df: pd.DataFrame, i: int, trade: Trade) -> str | None:
        row = df.iloc[i]
        if not pd.isna(row["dc_low"]) and row["close"] < row["dc_low"]:
            return "trend_exit"
        return None

    def trail(self, df: pd.DataFrame, i: int, trade: Trade) -> float | None:
        if not trade.entry_time:
            return None
        idx = df.index
        bar_start = pd.Timestamp(trade.entry_time).floor(f"{SECONDS[self.signal_tf]}s")
        since = df[(idx >= bar_start) & (idx <= idx[i])]
        if since.empty:
            return None
        chandelier = since["high"].max() - self.p.atr_mult * df["atr"].iloc[i]
        return chandelier if chandelier > trade.stop else None


def trend_risk_overrides(cfg: Config) -> dict:
    """Limites de risque de la poche tendance (bougies journalières)."""
    t = cfg.trend
    return {
        "risk_per_trade": t.risk_per_trade,
        "max_position_pct": t.max_asset_pct,
        "max_open_positions": max(len(t.products), 1),
        "max_trades_per_day": max(len(t.products), 1),
        "max_daily_loss": 0.05,
        # Un suivi de tendance traverse normalement des creux de 20-30 % : on coupe
        # les nouvelles entrées au-delà de 35 % de baisse de la poche.
        "max_drawdown": 0.35,
    }
