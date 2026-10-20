"""CLI : python -m daybot {backtest|paper|live|status|export-tax|keychain-import|keychain-forget}"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time

import pandas as pd

from . import backtest, config, credentials, market
from .broker import LiveBroker, PaperBroker
from .engine import Engine
from .journal import Journal
from .strategy import features

log = logging.getLogger("daybot")


def _closed_only(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Retire la bougie en cours (non clôturée)."""
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=market.SECONDS[tf])
    return df[df.index <= cutoff]


def run_loop(cfg: config.Config, broker, journal: Journal) -> None:
    eng = Engine(cfg, broker, journal)
    step = market.SECONDS[cfg.signal_tf]
    last_seen: dict[str, pd.Timestamp] = {}
    log.info("Démarrage %s sur %s (bougies %s)", cfg.mode, ", ".join(cfg.products), cfg.signal_tf)
    while True:
        for p in cfg.products:
            try:
                sig = _closed_only(market.candles(p, cfg.signal_tf, 300), cfg.signal_tf)
                tr = _closed_only(market.candles(p, cfg.trend_tf, 300), cfg.trend_tf)
                df = features(sig, tr, cfg)
                if df.empty or last_seen.get(p) == df.index[-1]:
                    continue
                last_seen[p] = df.index[-1]
                eng.on_bar(p, df, len(df) - 1)
            except Exception:  # noqa: BLE001 — une paire en erreur ne bloque pas les autres
                log.exception("%s erreur de traitement", p)
        if eng.risk.halted_reason:
            log.error(eng.risk.halted_reason)
        log.info("Capital estimé : %.2f %s", broker.equity(eng.prices), cfg.quote_currency)
        # Réveil 10 s après la prochaine clôture de bougie.
        time.sleep(step - time.time() % step + 10)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="daybot")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=int, default=60)
    sub.add_parser("paper")
    lv = sub.add_parser("live")
    lv.add_argument("--live", action="store_true", help="confirmation explicite du trading réel")
    sub.add_parser("status")
    ex = sub.add_parser("export-tax")
    ex.add_argument("--out", default=None, help="défaut : data/cessions_2086.csv")
    ki = sub.add_parser("keychain-import", help="range une clé CDP JSON dans le Trousseau macOS")
    ki.add_argument("key_file")
    sub.add_parser("keychain-forget", help="supprime la clé du Trousseau")
    a = ap.parse_args(argv)

    cfg = config.load(a.config)
    os.makedirs(cfg.data_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.handlers.RotatingFileHandler(
            os.path.join(cfg.data_dir, "daybot.log"), maxBytes=5_000_000, backupCount=3,
            encoding="utf-8")])

    if a.cmd == "backtest":
        logging.getLogger("daybot").setLevel(logging.WARNING)
        data = backtest.load_data(cfg, a.days)
        res = backtest.run(cfg, data)
        gross = backtest.run(cfg, data, no_fees=True)
        res["rendement_sans_frais_pct"] = gross["rendement_pct"]
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if a.cmd == "paper":
        cfg.mode = "paper"
        journal = Journal(cfg.journal_path.replace(".sqlite", "_paper.sqlite"))
        broker = PaperBroker(cfg)
        broker.restore(journal)          # reprend le portefeuille fictif après un arrêt
        run_loop(cfg, broker, journal)

    if a.cmd == "live":
        if not (a.live and cfg.mode == "live" and os.getenv("DAYBOT_LIVE_CONFIRM") == "OUI"):
            print("Trading réel refusé. Il faut : mode = \"live\" dans config.toml, l'option --live, "
                  "et DAYBOT_LIVE_CONFIRM=OUI dans l'environnement.", file=sys.stderr)
            return 2
        broker = LiveBroker(cfg)
        log.warning("MODE RÉEL — solde %s disponible : %.2f", cfg.quote_currency, broker.available_quote())
        run_loop(cfg, broker, Journal(cfg.journal_path))

    if a.cmd == "status":
        for path in (cfg.journal_path, cfg.journal_path.replace(".sqlite", "_paper.sqlite")):
            if not os.path.exists(path):
                continue
            j = Journal(path)
            closed = j.closed()
            print(f"{path}: {len(closed)} trades clôturés, P&L net {sum(t.pnl for t in closed):.2f}")
            for t in j.active():
                print(f"  {t.status} {t.product} entrée {t.entry_price or t.entry_limit:.4f} "
                      f"stop {t.stop:.4f} TP {t.target:.4f}")

    if a.cmd == "export-tax":
        out = a.out or os.path.join(cfg.data_dir, "cessions_2086.csv")
        n = Journal(cfg.journal_path).export_tax_csv(out)
        print(f"{n} cessions exportées vers {out}")

    if a.cmd == "keychain-import":
        name = credentials.import_key_file(a.key_file)
        print(f"Clé {name.split('/')[-1]} enregistrée dans le Trousseau (service "
              f"« {credentials.SERVICE} »). Vous pouvez supprimer {a.key_file}.")

    if a.cmd == "keychain-forget":
        credentials.forget()
        print("Clé supprimée du Trousseau.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
