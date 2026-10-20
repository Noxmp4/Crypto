"""Données de marché : endpoints publics Coinbase Advanced (aucune clé requise)."""
from __future__ import annotations

import time

import pandas as pd
import requests

BASE = "https://api.coinbase.com/api/v3/brokerage/market"
SECONDS = {
    "ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600, "TWO_HOUR": 7200, "SIX_HOUR": 21600, "ONE_DAY": 86400,
}
MAX_PER_CALL = 300

# Une seule session HTTP réutilisée : sans elle, chaque requête refait une poignée de
# main TLS complète, ce qui rendait le téléchargement d'un long historique très lent.
_session = requests.Session()
_session.headers["User-Agent"] = "daybot/1.0"


def _get(path: str, params: dict | None = None, retries: int = 4) -> dict:
    for i in range(retries):
        try:
            r = _session.get(f"{BASE}{path}", params=params, timeout=15)
            if r.status_code == 429:
                raise requests.HTTPError("rate limited")
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == retries - 1:
                raise
            time.sleep(2 ** i)
    raise RuntimeError("unreachable")


def candles(product: str, granularity: str, bars: int = 300, end: int | None = None) -> pd.DataFrame:
    """Retourne `bars` bougies OHLCV triées par date croissante, index UTC."""
    step = SECONDS[granularity]
    end = end or int(time.time())
    rows: list[dict] = []
    while len(rows) < bars:
        n = min(MAX_PER_CALL, bars - len(rows))
        start = end - n * step
        data = _get(f"/products/{product}/candles",
                    {"start": start, "end": end, "granularity": granularity, "limit": n})
        chunk = data.get("candles", [])
        if not chunk:
            break
        rows.extend(chunk)
        end = start
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["start"].astype(int), unit="s", utc=True)
    df = df.drop(columns="start").set_index("time").astype(float)
    return df[~df.index.duplicated()].sort_index()


def product(product_id: str) -> dict:
    return _get(f"/products/{product_id}")

