"""Stratégie intraday v1 (15 min, filtre 1 h), inchangée — désactivée par défaut :
le backtest montre qu'elle perd à cause des frais du palier Intro."""
from __future__ import annotations

import pandas as pd

from .. import strategy as legacy
from ..config import Config
from .base import Strategy


class IntradayStrategy(Strategy):
    name = "intraday"
    entry_mode = "maker"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        s = cfg.strategy
        self.signal_tf = cfg.signal_tf
        self.entry_ttl_bars = s.entry_ttl_bars
        self.max_hold_bars = s.max_hold_bars
        self.warmup = max(s.ema_slow, s.atr_period, s.rsi_period) * 3

    def frames(self) -> dict[str, int]:
        return {self.cfg.signal_tf: 300, self.cfg.trend_tf: 300}

    def features(self, frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
        return legacy.features(frames[self.cfg.signal_tf], frames[self.cfg.trend_tf], self.cfg)

    def signal_at(self, df, i, product):
        return legacy.signal_at(df, i, product, self.cfg)
