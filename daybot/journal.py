"""Journal SQLite : état des trades (reprise après redémarrage) + export fiscal."""
from __future__ import annotations

import csv
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

    @property
    def pnl(self) -> float:
        if self.status != "CLOSED":
            return 0.0
        return (self.exit_price - self.entry_price) * self.base_size - self.entry_fee - self.exit_fee


_COLS = [f.name for f in fields(Trade) if f.name != "id"]


class Journal:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        cols = ", ".join(f"{c} {'REAL' if c in _REAL else 'TEXT'}" for c in _COLS)
        self.db.execute(f"CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, {cols})")
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

    def active(self, product: str | None = None) -> list[Trade]:
        if product:
            return self._load("status IN ('PENDING','OPEN') AND product=?", (product,))
        return self._load("status IN ('PENDING','OPEN')")

    def closed(self) -> list[Trade]:
        return self._load("status='CLOSED'")

    def day_stats(self, day: str) -> tuple[int, float]:
        """(nombre d'entrées remplies ce jour, P&L réalisé ce jour). `day` = AAAA-MM-JJ UTC."""
        entries = self._load("entry_time LIKE ?", (f"{day}%",))
        exits = self._load("status='CLOSED' AND exit_time LIKE ?", (f"{day}%",))
        return len(entries), sum(t.pnl for t in exits)

    def export_tax_csv(self, path: str) -> int:
        """Liste des cessions crypto → EUR (faits générateurs, formulaire 2086).

        Le calcul de plus-value française (art. 150 VH bis) utilise la valeur
        GLOBALE du portefeuille à chaque cession : ce fichier fournit les
        cessions, prix et frais, à compléter avec vos autres avoirs.
        """
        trades = self.closed()
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
         "exit_price", "exit_fee", "bars_waiting", "bars_held"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
