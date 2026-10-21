"""Journal SQLite : état des trades (reprise après redémarrage) + export fiscal.

Un fichier par mode (paper / live) pour les trades, lots DCA et courbes de capital,
et un fichier « control » partagé avec le site web (commandes, analyses IA, événements).
Toutes les tables existent dans chaque fichier ; seules celles utiles sont remplies.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Trade:
    product: str
    signal_time: str
    entry_limit: float
    stop: float
    target: float
    base_size: float
    status: str = "PENDING"          # PENDING → OPEN → CLOSED | CANCELLED
    id: int | None = None
    entry_order_id: str = ""
    entry_price: float = 0.0
    entry_fee: float = 0.0
    entry_time: str = ""
    exit_order_id: str = ""
    exit_price: float = 0.0
    exit_fee: float = 0.0
    exit_time: str = ""
    exit_reason: str = ""
    bars_waiting: int = 0
    bars_held: int = 0
    sleeve: str = "intraday"
    initial_stop: float = 0.0        # stop d'origine (le stop suiveur fait monter `stop`)
    entry_mode: str = "maker"        # maker = post-only ; marketable = limite traversante

    @property
    def has_target(self) -> bool:
        return bool(self.target) and self.target > 0

    @property
    def pnl(self) -> float:
        if self.status != "CLOSED":
            return 0.0
        return (self.exit_price - self.entry_price) * self.base_size - self.entry_fee - self.exit_fee


@dataclass
class DcaLot:
    product: str
    created: str
    quote_amount: float
    limit_price: float
    base_size: float
    status: str = "PENDING"          # PENDING → FILLED | CANCELLED
    id: int | None = None
    order_id: str = ""
    placed_at: str = ""
    post_only: int = 1
    price: float = 0.0
    fee: float = 0.0
    filled_time: str = ""
    reprices: int = 0

    @property
    def cost(self) -> float:
        return self.price * self.base_size + self.fee


_COLS = [f.name for f in fields(Trade) if f.name != "id"]
_LOT_COLS = [f.name for f in fields(DcaLot) if f.name != "id"]


def _coltype(c: str) -> str:
    return "REAL" if c in _REAL else "TEXT"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY, created TEXT, kind TEXT,
    payload TEXT, author TEXT, status TEXT DEFAULT 'NEW', result TEXT, done_at TEXT);
CREATE TABLE IF NOT EXISTS analyses (id INTEGER PRIMARY KEY, created TEXT, kind TEXT,
    question TEXT, result TEXT, model TEXT, cost_usd REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, created TEXT, level TEXT,
    source TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS equity (id INTEGER PRIMARY KEY, time TEXT, sleeve TEXT, value REAL);
CREATE TABLE IF NOT EXISTS ai_usage (id INTEGER PRIMARY KEY, time TEXT, kind TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
    web_searches INTEGER, cost_usd REAL);
"""


class Journal:
    def __init__(self, path: str = ":memory:", readonly: bool = False):
        if path != ":memory:" and not readonly:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{path}?mode=ro" if readonly else path
        self.db = sqlite3.connect(uri, uri=readonly, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if readonly:
            return
        if path != ":memory:":
            # WAL : le site web lit pendant que le bot écrit.
            self.db.execute("PRAGMA journal_mode=WAL")
        cols = ", ".join(f"{c} {_coltype(c)}" for c in _COLS)
        self.db.execute(f"CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, {cols})")
        lot_cols = ", ".join(f"{c} {'REAL' if c in _LOT_REAL else 'TEXT'}" for c in _LOT_COLS)
        self.db.execute(f"CREATE TABLE IF NOT EXISTS dca_lots (id INTEGER PRIMARY KEY, {lot_cols})")
        self.db.executescript(_SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Ajoute les colonnes apparues depuis la v1 (journaux existants)."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(trades)")}
        defaults = {f.name: f.default for f in fields(Trade)}
        for c in _COLS:
            if c not in have:
                d = defaults[c]
                lit = repr(d) if isinstance(d, (int, float)) else f"'{d}'"
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {c} {_coltype(c)} DEFAULT {lit}")

    def get_state(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, str(value)))
        self.db.commit()

    def save(self, t: Trade) -> Trade:
        d = asdict(t)
        if t.id is None:
            cur = self.db.execute(
                f"INSERT INTO trades ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
                [d[c] for c in _COLS])
            t.id = cur.lastrowid
        else:
            self.db.execute(f"UPDATE trades SET {', '.join(f'{c}=?' for c in _COLS)} WHERE id=?",
                            [d[c] for c in _COLS] + [t.id])
        self.db.commit()
        return t

    def _load(self, where: str, args: tuple = ()) -> list[Trade]:
        rows = self.db.execute(f"SELECT * FROM trades WHERE {where} ORDER BY id", args).fetchall()
        return [Trade(**{k: r[k] for k in r.keys()}) for r in rows]

    def active(self, product: str | None = None, sleeve: str | None = None) -> list[Trade]:
        where, args = "status IN ('PENDING','OPEN')", []
        if product:
            where += " AND product=?"; args.append(product)
        if sleeve:
            where += " AND sleeve=?"; args.append(sleeve)
        return self._load(where, tuple(args))

    def closed(self, sleeve: str | None = None) -> list[Trade]:
        if sleeve:
            return self._load("status='CLOSED' AND sleeve=?", (sleeve,))
        return self._load("status='CLOSED'")

    def trade(self, trade_id: int) -> Trade | None:
        rows = self._load("id=?", (trade_id,))
        return rows[0] if rows else None

    def recent_trades(self, limit: int = 50) -> list[Trade]:
        rows = self.db.execute("SELECT * FROM trades WHERE status != 'CANCELLED' "
                               "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Trade(**{k: r[k] for k in r.keys()}) for r in rows]

    def day_stats(self, day: str, sleeve: str | None = None) -> tuple[int, float]:
        """(nombre d'entrées remplies ce jour, P&L réalisé ce jour). `day` = AAAA-MM-JJ UTC."""
        extra, args = (" AND sleeve=?", (sleeve,)) if sleeve else ("", ())
        entries = self._load("entry_time LIKE ?" + extra, (f"{day}%", *args))
        exits = self._load("status='CLOSED' AND exit_time LIKE ?" + extra, (f"{day}%", *args))
        return len(entries), sum(t.pnl for t in exits)

    # ── lots DCA ──
    def save_lot(self, lot: DcaLot) -> DcaLot:
        d = asdict(lot)
        if lot.id is None:
            cur = self.db.execute(
                f"INSERT INTO dca_lots ({', '.join(_LOT_COLS)}) VALUES "
                f"({', '.join('?' * len(_LOT_COLS))})", [d[c] for c in _LOT_COLS])
            lot.id = cur.lastrowid
        else:
            self.db.execute(f"UPDATE dca_lots SET {', '.join(f'{c}=?' for c in _LOT_COLS)} "
                            "WHERE id=?", [d[c] for c in _LOT_COLS] + [lot.id])
        self.db.commit()
        return lot

    def lots(self, status: str | None = None) -> list[DcaLot]:
        q, args = "SELECT * FROM dca_lots", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [DcaLot(**{k: r[k] for k in r.keys()}) for r in rows]

    def dca_holdings(self) -> dict[str, dict]:
        """Par paire : quantité, coût total (frais inclus) et prix moyen d'achat."""
        out: dict[str, dict] = {}
        for lot in self.lots("FILLED"):
            h = out.setdefault(lot.product, {"qty": 0.0, "cost": 0.0})
            h["qty"] += lot.base_size
            h["cost"] += lot.cost
        for h in out.values():
            h["avg_price"] = h["cost"] / h["qty"] if h["qty"] else 0.0
        return out

    # ── commandes du site web ──
    def add_command(self, kind: str, payload: dict | None = None, author: str = "") -> int:
        cur = self.db.execute("INSERT INTO commands (created, kind, payload, author) VALUES "
                              "(?,?,?,?)", (iso_now(), kind, json.dumps(payload or {}), author))
        self.db.commit()
        return cur.lastrowid

    def pending_commands(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM commands WHERE status='NEW' ORDER BY id").fetchall()

    def finish_command(self, cmd_id: int, ok: bool, result: str) -> None:
        self.db.execute("UPDATE commands SET status=?, result=?, done_at=? WHERE id=?",
                        ("DONE" if ok else "ERROR", result, iso_now(), cmd_id))
        self.db.commit()

    def commands(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ── événements, capital, analyses ──
    def event(self, level: str, source: str, message: str) -> None:
        self.db.execute("INSERT INTO events (created, level, source, message) VALUES (?,?,?,?)",
                        (iso_now(), level, source, message[:2000]))
        self.db.commit()

    def events(self, limit: int = 50, level: str | None = None) -> list[sqlite3.Row]:
        if level:
            return self.db.execute("SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?",
                                   (level, limit)).fetchall()
        return self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def count_events(self, level: str, source: str, since: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events WHERE level=? AND source=? AND "
                               "created>=?", (level, source, since)).fetchone()[0]

    def snapshot(self, sleeve: str, value: float, when: str | None = None) -> None:
        self.db.execute("INSERT INTO equity (time, sleeve, value) VALUES (?,?,?)",
                        (when or iso_now(), sleeve, value))
        self.db.commit()

    def equity_curve(self, sleeve: str, since: str = "") -> list[tuple[str, float]]:
        return [(r["time"], r["value"]) for r in self.db.execute(
            "SELECT time, value FROM equity WHERE sleeve=? AND time>=? ORDER BY time",
            (sleeve, since))]

    def save_analysis(self, kind: str, result: dict | str, model: str, cost: float,
                      question: str = "", when: str | None = None) -> int:
        body = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        cur = self.db.execute("INSERT INTO analyses (created, kind, question, result, model, "
                              "cost_usd) VALUES (?,?,?,?,?,?)",
                              (when or iso_now(), kind, question, body, model, cost))
        self.db.commit()
        return cur.lastrowid

    def analyses(self, kind: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if kind:
            return self.db.execute("SELECT * FROM analyses WHERE kind=? ORDER BY id DESC LIMIT ?",
                                   (kind, limit)).fetchall()
        return self.db.execute("SELECT * FROM analyses ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def record_ai_usage(self, kind: str, model: str, usage: dict, cost: float,
                        when: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO ai_usage (time, kind, model, input_tokens, output_tokens, cache_read, "
            "cache_write, web_searches, cost_usd) VALUES (?,?,?,?,?,?,?,?,?)",
            (when or iso_now(), kind, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
             usage.get("cache_read", 0), usage.get("cache_write", 0), usage.get("web_searches", 0),
             cost))
        self.db.commit()

    def ai_spent(self, since: str) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE "
                                     "time>=?", (since,)).fetchone()[0])

    def ai_count(self, kind: str, since: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM analyses WHERE kind=? AND created>=?",
                               (kind, since)).fetchone()[0]

    def get_json(self, key: str, default=None):
        raw = self.get_state(key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, key: str, value) -> None:
        self.set_state(key, json.dumps(value, ensure_ascii=False))

    def export_tax_csv(self, path: str) -> int:
        """Liste des cessions crypto → EUR (faits générateurs, formulaire 2086).

        Le calcul de plus-value française (art. 150 VH bis) utilise la valeur
        GLOBALE du portefeuille à chaque cession : ce fichier fournit les
        cessions, prix et frais, à compléter avec vos autres avoirs.
        """
        trades = self.closed()          # les achats DCA ne sont pas des cessions
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["date_cession_utc", "actif", "quantite", "prix_cession_unitaire",
                        "prix_cession_total", "frais_cession", "date_acquisition_utc",
                        "prix_acquisition_total", "frais_acquisition", "pnl_net_trade"])
            for t in trades:
                w.writerow([t.exit_time, t.product.split("-")[0], f"{t.base_size:.8f}",
                            f"{t.exit_price:.6f}", f"{t.exit_price * t.base_size:.2f}",
                            f"{t.exit_fee:.2f}", t.entry_time,
                            f"{t.entry_price * t.base_size:.2f}", f"{t.entry_fee:.2f}",
                            f"{t.pnl:.2f}"])
        return len(trades)


_REAL = {"entry_limit", "stop", "target", "base_size", "entry_price", "entry_fee",
         "exit_price", "exit_fee", "bars_waiting", "bars_held", "initial_stop"}
_LOT_REAL = {"quote_amount", "limit_price", "base_size", "post_only", "price", "fee", "reprices"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
