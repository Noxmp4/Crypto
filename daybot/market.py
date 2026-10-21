"""Données de marché : endpoints publics Coinbase Advanced (aucune clé requise).

`history()` s'appuie sur un cache SQLite incrémental : seules les bougies manquantes
sont téléchargées, et seules les bougies *clôturées* sont mises en cache.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd
import requests

BASE = "https://api.coinbase.com/api/v3/brokerage/market"
SECONDS = {
    "ONE_MINUTE": 60, "FIVE_MINUTE": 300, "FIFTEEN_MINUTE": 900, "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600, "TWO_HOUR": 7200, "FOUR_HOUR": 14400, "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
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


class CandleCache:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("""CREATE TABLE IF NOT EXISTS candles (
            product TEXT, granularity TEXT, start INTEGER, open REAL, high REAL, low REAL,
            close REAL, volume REAL, PRIMARY KEY (product, granularity, start))""")
        # Plus ancienne date déjà demandée : évite de redemander chaque fois la période
        # antérieure à la cotation d'une paire (ex. SOL-EUR avant 2021).
        self.db.execute("""CREATE TABLE IF NOT EXISTS coverage (
            product TEXT, granularity TEXT, first_requested INTEGER,
            PRIMARY KEY (product, granularity))""")
        self.db.commit()

    def bounds(self, product: str, gran: str) -> tuple[int | None, int | None, int | None]:
        lo, hi = self.db.execute("SELECT MIN(start), MAX(start) FROM candles WHERE product=? "
                                 "AND granularity=?", (product, gran)).fetchone()
        row = self.db.execute("SELECT first_requested FROM coverage WHERE product=? AND "
                              "granularity=?", (product, gran)).fetchone()
        return lo, hi, (row[0] if row else None)

    def put(self, product: str, gran: str, rows: list[dict]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
            [(product, gran, int(r["start"]), float(r["open"]), float(r["high"]), float(r["low"]),
              float(r["close"]), float(r["volume"])) for r in rows])
        self.db.commit()

    def requested_from(self, product: str, gran: str, start: int) -> None:
        self.db.execute("INSERT INTO coverage VALUES (?,?,?) ON CONFLICT(product, granularity) "
                        "DO UPDATE SET first_requested=MIN(first_requested, excluded.first_requested)",
                        (product, gran, start))
        self.db.commit()

    def load(self, product: str, gran: str, start: int, end: int) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT start, open, high, low, close, volume FROM candles WHERE product=? AND "
            "granularity=? AND start>=? AND start<=? ORDER BY start",
            self.db, params=(product, gran, start, end))
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["start"].astype("int64"), unit="s", utc=True)
        return df.drop(columns="start").set_index("time").astype(float)


def _fetch_range(product: str, gran: str, start: int, end: int) -> list[dict]:
    """Bougies de [start, end], par fenêtres de MAX_PER_CALL, en avançant dans le temps."""
    step = SECONDS[gran]
    rows: list[dict] = []
    s = start
    while s < end:
        e = min(s + MAX_PER_CALL * step, end)
        data = _get(f"/products/{product}/candles",
                    {"start": s, "end": e, "granularity": gran, "limit": MAX_PER_CALL})
        rows.extend(data.get("candles", []))
        s = e
    return rows


def history(product: str, granularity: str, start: int, end: int | None = None,
            cache_path: str | None = None, now: float | None = None) -> pd.DataFrame:
    """Bougies *clôturées* de `start` à `end` (timestamps UTC), via le cache si fourni."""
    step = SECONDS[granularity]
    now = int(now or time.time())
    last_closed = (now // step) * step - step          # début de la dernière bougie clôturée
    end = min(end or last_closed, last_closed)
    start = (start // step) * step
    if cache_path is None:
        rows = [r for r in _fetch_range(product, granularity, start, end + step)
                if int(r["start"]) <= end]
        return _frame(rows)
    cache = CandleCache(cache_path)
    lo, hi, first_req = cache.bounds(product, granularity)
    if hi is None:
        todo = [(start, end + step)]
    else:
        todo = []
        if first_req is None or start < first_req:
            todo.append((start, lo))
        if hi < end:
            todo.append((hi + step, end + step))
    for a, b in todo:
        rows = [r for r in _fetch_range(product, granularity, a, b)
                if int(r["start"]) <= last_closed]
        cache.put(product, granularity, rows)
    cache.requested_from(product, granularity, start)
    return cache.load(product, granularity, start, end)


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["start"].astype(int), unit="s", utc=True)
    df = df.drop(columns="start").set_index("time").astype(float)
    return df[~df.index.duplicated()].sort_index()


def last_price(product_id: str) -> float:
    return float(product(product_id).get("price") or 0)


def product(product_id: str) -> dict:
    return _get(f"/products/{product_id}")

