"""Stratégies « à position » : une entrée, un stop protégé sur l'exchange, une sortie."""
from .base import Strategy
from .intraday import IntradayStrategy
from .trend import TrendStrategy

__all__ = ["Strategy", "IntradayStrategy", "TrendStrategy"]
