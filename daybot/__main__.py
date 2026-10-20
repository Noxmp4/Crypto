"""CLI : python -m daybot {backtest|paper|live|status|export-tax|keychain-import|keychain-forget}"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
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
    # Ctrl+C / SIGTERM : on termine la paire en cours avant de sortir, pour ne jamais
    # quitter entre l'envoi d'un ordre et son enregistrement dans le journal.
    stop = threading.Event()

    def _ask_stop(signum, _frame):
        if stop.is_set():                    # second Ctrl+C : sortie immédiate
            raise KeyboardInterrupt
        log.info("Arrêt demandé — fin du traitement en cours…")
        stop.set()

    signal.signal(signal.SIGINT, _ask_stop)
    signal.signal(signal.SIGTERM, _ask_stop)
    log.info("Démarrage %s sur %s (bougies %s) — Ctrl+C pour arrêter",
             cfg.mode, ", ".join(cfg.products), cfg.signal_tf)
    while not stop.is_set():
        for p in cfg.products:
            if stop.is_set():
                break
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
        # Réveil 10 s après la prochaine clôture de bougie (interrompu par Ctrl+C).
        stop.wait(step - time.time() % step + 10)
    log.info("Bot arrêté. État sauvegardé dans %s — relancez pour reprendre.", cfg.data_dir)


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
        # Progression sur stderr : stdout ne contient que le JSON final.
        def say(msg: str) -> None:
            print(msg, file=sys.stderr, flush=True)

        t0 = time.time()
        say(f"Backtest {a.days} jours sur {', '.join(cfg.products)} — compter 1 à 3 minutes.")
        data = backtest.load_data(cfg, a.days, progress=say)
        res = backtest.run(cfg, data, progress=say)
        gross = backtest.run(cfg, data, no_fees=True, progress=say)
        say(f"Terminé en {time.time() - t0:.0f} s.")
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
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Le journal est écrit à chaque événement : rien n'est perdu. En live, les
        # ordres stop/TP restent actifs chez Coinbase.
        print("\nArrêt demandé (Ctrl+C). État sauvegardé dans data/ — relancez pour reprendre.",
              file=sys.stderr)
        sys.exit(130)
