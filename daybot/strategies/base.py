"""Interface commune des stratégies utilisées par `Engine`."""
from __future__ import annotations

import pandas as pd

from ..journal import Trade
from ..strategy import Signal


class Strategy:
    """À sous-classer. Toutes les méthodes reçoivent les features et l'index d'une
    bougie *clôturée* ; aucune ne doit lire au-delà de `i` (pas de look-ahead)."""

    name = "base"
    signal_tf = "ONE_DAY"
    entry_mode = "maker"           # maker = post-only ; marketable = limite traversante (taker)
    entry_ttl_bars = 1
    max_hold_bars: int | None = None
    warmup = 0

    def frames(self) -> dict[str, int]:
        """Timeframes nécessaires → nombre de bougies d'historique pour le live."""
        return {self.signal_tf: 300}

    def features(self, frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
        raise NotImplementedError

    def signal_at(self, df: pd.DataFrame, i: int, product: str) -> Signal | None:
        raise NotImplementedError

    def exit_at(self, df: pd.DataFrame, i: int, trade: Trade) -> str | None:
        """Raison de sortie sur signal (vente au marché), ou None."""
        return None

    def trail(self, df: pd.DataFrame, i: int, trade: Trade) -> float | None:
        """Nouveau stop (uniquement s'il monte), ou None."""
        return None
