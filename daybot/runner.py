"""Processus permanent (`python -m daybot run`) : paper « ombre » + poches promues en réel,
planificateur, analyste IA, commandes du site web, réconciliation, sauvegardes.

Le paper tourne toujours. Une poche passe en réel quand `promotion.evaluate` la juge
éligible ; elle y reste tant que l'accord, la validation et l'absence de
rétrogradation tiennent. Toutes les erreurs deviennent des événements visibles sur le site.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import backtest, market, promotion
from .analyst import Analyst, AnalystError, active_controls, technicals
from .broker import PaperBroker
from .config import Config
from .credentials import anthropic_key
from .engine import Engine
from .journal import Journal
from .risk import Controls
from .scheduler import Daily, Every, Scheduler, Weekly
from .sleeves.dca import DcaSleeve
from .strategies import IntradayStrategy, TrendStrategy
from .strategies.trend import trend_risk_overrides

log = logging.getLogger("daybot")
UTC = timezone.utc


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Runner:
    def __init__(self, cfg: Config, live_broker_factory=None, analyst: Analyst | None = None,
                 now=lambda: datetime.now(UTC)):
        self.cfg, self.now = cfg, now
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.control = Journal(cfg.control_path)
        self.paper_j = Journal(cfg.paper_journal_path)
        self.paper = PaperBroker(cfg)
        self.paper.restore(self.paper_j)
        self.live_j: Journal | None = Journal(cfg.journal_path) if os.path.exists(cfg.journal_path) else None
        self.live = None
        self._live_factory = live_broker_factory
        self.prices: dict[str, float] = {}
        self.strategies = {}
        if cfg.trend.enabled:
            self.strategies["trend"] = TrendStrategy(cfg)
        if cfg.intraday_enabled:
            self.strategies["intraday"] = IntradayStrategy(cfg)
        self.engines: dict[str, dict[str, Engine]] = {"paper": {}, "live": {}}
        for name, strat in self.strategies.items():
            self.engines["paper"][name] = self._engine("paper", strat)
        self.dca: dict[str, DcaSleeve] = {}
        if cfg.dca.enabled:
            self.dca["paper"] = DcaSleeve(cfg, self.paper, self.paper_j, label="paper:dca")
        self.analyst = analyst
        if self.analyst is None and cfg.ai.enabled and anthropic_key():
            self.analyst = Analyst(cfg, self.control)
        for sleeve in promotion.enabled_sleeves(cfg):
            key = f"paper_started:{sleeve}"
            if not self.paper_j.get_state(key):
                self.paper_j.set_state(key, _iso(self.now()))
        self.promo: dict = {}
        self.sched = Scheduler(self.control, on_error=self._job_error)
        self._register_jobs()

    # ── construction ──
    def _engine(self, mode: str, strat) -> Engine:
        broker, journal = (self.paper, self.paper_j) if mode == "paper" else (self.live, self.live_j)
        capital = self.cfg.trend.capital_eur if strat.name == "trend" else self.cfg.intraday_capital_eur
        overrides = trend_risk_overrides(self.cfg) if strat.name == "trend" else None
        deployable = None
        if mode == "live":
            deployable = lambda: promotion.deployable_eur(self.cfg, self.live_j)  # noqa: E731
        return Engine(self.cfg, broker, journal, strat, capital=capital, risk_overrides=overrides,
                      controls=lambda: self.controls(mode), deployable=deployable,
                      label=f"{mode}:{strat.name}")

    def ensure_live(self) -> bool:
        if self.live is not None:
            return True
        try:
            if self._live_factory:
                self.live = self._live_factory(self.cfg)
            else:
                from .broker import LiveBroker
                self.live = LiveBroker(self.cfg)
        except Exception as e:  # noqa: BLE001
            self.event("ERROR", "live", f"connexion Coinbase impossible : {e}")
            return False
        if self.live_j is None:
            self.live_j = Journal(self.cfg.journal_path)
        for name, strat in self.strategies.items():
            self.engines["live"][name] = self._engine("live", strat)
        if self.cfg.dca.enabled:
            self.dca["live"] = DcaSleeve(
                self.cfg, self.live, self.live_j, label="live:dca",
                deployable=lambda: promotion.deployable_eur(self.cfg, self.live_j),
                allowed=lambda: self.live_allowed("dca"))
        self.event("INFO", "live", "connexion au compte Coinbase réel établie")
        return True

    # ── état partagé ──
    def event(self, level: str, source: str, msg: str) -> None:
        getattr(log, {"ERROR": "error", "WARNING": "warning"}.get(level, "info"))("[%s] %s", source, msg)
        self.control.event(level, source, msg)

    def _job_error(self, name: str, e: BaseException) -> None:
        log.exception("tâche %s en échec", name)
        self.event("ERROR", f"job:{name}", f"{type(e).__name__}: {e}")

    def live_allowed(self, sleeve: str) -> bool:
        return bool((self.promo.get(sleeve) or {}).get("live_allowed"))

    def controls(self, mode: str) -> Controls:
        mult, blackouts = active_controls(self.control, self.now())
        blocked = set()
        if mode == "live":
            blocked = {s for s in self.strategies if not self.live_allowed(s)}
        return Controls(paused=self.control.get_state("paused") == "1", entries_blocked=blocked,
                        risk_multiplier=mult, blackouts=blackouts)

    # ── tâches ──
    def _register_jobs(self) -> None:
        c, tz, s = self.cfg, self.cfg.timezone, self.sched
        s.add("heartbeat", Every(60), self.heartbeat, run_at_start=True)
        s.add("prices", Every(300), self.refresh_prices, run_at_start=True)
        s.add("research", Weekly(6, 3, 0, tz), self.research,
              run_at_start=not self.control.get_json("research"))
        s.add("promotion", Every(600), self.update_promotion, run_at_start=True)
        if "trend" in self.strategies:
            s.add("bars:trend", Daily(0, 5, "UTC"), lambda: self.process_bars("trend"),
                  run_at_start=True)
        if "intraday" in self.strategies:
            s.add("bars:intraday", Every(900, offset=10), lambda: self.process_bars("intraday"),
                  run_at_start=True)
        if c.dca.enabled:
            s.add("dca:buy", Weekly(c.dca.weekday, c.dca.hour, c.dca.minute, tz), self.dca_buy)
            s.add("dca:manage", Every(300), self.dca_manage)
        if self.analyst:
            s.add("ai:daily", Daily(c.ai.daily_hour, c.ai.daily_minute, tz), self.ai_report)
        s.add("reconcile", Every(3600), self.reconcile, run_at_start=True)
        s.add("fees", Daily(3, 0, tz), self.refresh_fees, run_at_start=True)
        s.add("equity", Every(3600), self.snapshot, run_at_start=True)
        s.add("backup", Daily(3, 30, tz), self.backup)

    def heartbeat(self) -> None:
        now = self.now()
        self.control.set_state("heartbeat", _iso(now))
        Path(self.cfg.path("heartbeat")).write_text(_iso(now))
        self.process_commands()
        if self.live is not None:
            for name, eng in self.engines["live"].items():
                try:
                    eng.poll_live()
                except Exception as e:  # noqa: BLE001
                    self.event("ERROR", f"live:{name}", f"suivi des ordres : {e}")

    def refresh_prices(self) -> None:
        for p in self.cfg.all_products:
            try:
                self.prices[p] = market.last_price(p)
            except Exception as e:  # noqa: BLE001
                log.warning("prix %s indisponible : %s", p, e)
        for engines in self.engines.values():
            for eng in engines.values():
                eng.prices.update(self.prices)
        self.control.set_json("prices", {"time": _iso(self.now()), "prices": self.prices})

    def research(self) -> None:
        res = backtest.research(self.cfg, progress=lambda m: log.info("recherche : %s", m))
        self.control.set_json("research", res)
        oos = res["tendance"]["hors_echantillon"]
        self.event("INFO", "research", f"backtest hebdomadaire : tendance hors échantillon "
                   f"{oos['rendement_pct']} %, profit factor {oos['profit_factor']}")

    def update_promotion(self) -> None:
        now = self.now()
        res = promotion.evaluate(self.cfg, self.paper_j, self.control, now)
        for sleeve, r in res.items():
            if r["eligible"] and not r["live_since"]:
                self.control.set_state(f"live_since:{sleeve}", _iso(now))
                r["live_since"], r["live_allowed"] = _iso(now), True
                self.event("WARNING", "promotion",
                           f"poche {sleeve} promue en RÉEL (plafond {self.cfg.promotion.max_live_capital_eur:.0f} €)")
        self.promo = res
        self.control.set_json("promotion", {"date": _iso(now), "sleeves": res})
        if any(r["live_allowed"] for r in res.values()) or self._has_live_positions():
            self.ensure_live()

    def _has_live_positions(self) -> bool:
        return bool(self.live_j and (self.live_j.active() or self.live_j.lots("PENDING")))

    def _frames(self, strat) -> dict[str, pd.DataFrame]:
        out = {}
        now = self.now().timestamp()
        for p in self._products(strat):
            frames = {tf: market.history(p, tf, int(now) - n * market.SECONDS[tf],
                                         cache_path=self.cfg.market_cache_path, now=now)
                      for tf, n in strat.frames().items()}
            if all(len(f) for f in frames.values()):
                out[p] = strat.features(frames)
        return out

    def _products(self, strat) -> list[str]:
        return self.cfg.trend.products if strat.name == "trend" else self.cfg.products

    def process_bars(self, sleeve: str) -> None:
        """Traite la dernière bougie clôturée de chaque paire (une seule fois par bougie)."""
        strat = self.strategies[sleeve]
        data = self._frames(strat)
        for mode in ("paper", "live"):
            eng = self.engines[mode].get(sleeve)
            if eng is None:
                continue
            journal = self.paper_j if mode == "paper" else self.live_j
            for p, df in data.items():
                key = f"last_bar:{sleeve}:{p}"
                last = df.index[-1].isoformat()
                if journal.get_state(key) == last:
                    continue
                try:
                    eng.on_bar(p, df, len(df) - 1)
                except Exception as e:  # noqa: BLE001
                    self.event("ERROR", f"{mode}:{sleeve}", f"{p} : {type(e).__name__}: {e}")
                    continue
                journal.set_state(key, last)

    def _sma200(self) -> dict[str, float]:
        out = {}
        now = self.now().timestamp()
        for p in self.cfg.dca.weights:
            d = market.history(p, "ONE_DAY", int(now) - 260 * 86400,
                               cache_path=self.cfg.market_cache_path, now=now)
            if len(d) >= 200:
                out[p] = float(d["close"].rolling(200).mean().iloc[-1])
        return out

    def dca_buy(self) -> None:
        if not self.prices:
            self.refresh_prices()
        sma = self._sma200() if self.cfg.dca.tilt else None
        for mode, sleeve in self.dca.items():
            try:
                lots = sleeve.buy(self.prices, sma, now=self.now())
                if lots:
                    self.event("INFO", f"{mode}:dca", "achat hebdomadaire : " + ", ".join(
                        f"{lot.product} {lot.quote_amount:.2f} €" for lot in lots))
            except Exception as e:  # noqa: BLE001
                self.event("ERROR", f"{mode}:dca", f"achat : {type(e).__name__}: {e}")

    def _paper_bars(self, product: str, since: str) -> pd.DataFrame | None:
        start = int(datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp())
        df = market.history(product, "FIVE_MINUTE", start, now=self.now().timestamp())
        return df[df.index >= pd.Timestamp(since)] if not df.empty else df

    def dca_manage(self) -> None:
        for mode, sleeve in self.dca.items():
            try:
                bars = self._paper_bars if mode == "paper" else (lambda p, s: None)
                sleeve.manage(self.prices, bars, now=self.now())
            except Exception as e:  # noqa: BLE001
                self.event("ERROR", f"{mode}:dca", f"suivi : {type(e).__name__}: {e}")

    def reconcile(self) -> None:
        if self.live is None or self.live_j is None:
            return
        for note in self.live.reconcile(self.live_j):
            self.event("WARNING", "reconcile", note)

    def refresh_fees(self) -> None:
        if self.live is None:
            return
        tier = self.live.refresh_fees()
        self.control.set_json("fees", tier)

    def snapshot(self) -> None:
        if not self.prices:
            self.refresh_prices()
        when = _iso(self.now())
        for mode in ("paper", "live"):
            journal = self.paper_j if mode == "paper" else self.live_j
            broker = self.paper if mode == "paper" else self.live
            if journal is None or broker is None:
                continue
            journal.snapshot("total", broker.equity(self.prices), when)
            if "trend" in self.engines[mode]:
                journal.snapshot("trend", self.engines[mode]["trend"].sleeve_equity(), when)
            dca_value = sum(h["qty"] * self.prices.get(p, h["avg_price"])
                            for p, h in journal.dca_holdings().items())
            journal.snapshot("dca", dca_value, when)

    def backup(self) -> None:
        dest = Path(self.cfg.path("backups")) / self.now().strftime("%Y-%m-%d")
        dest.mkdir(parents=True, exist_ok=True)
        for src in (self.cfg.paper_journal_path, self.cfg.journal_path, self.cfg.control_path):
            if os.path.exists(src):
                with sqlite3.connect(src) as a, sqlite3.connect(dest / Path(src).name) as b:
                    a.backup(b)
        keep = sorted(Path(self.cfg.path("backups")).iterdir())[:-14]
        for old in keep:
            shutil.rmtree(old, ignore_errors=True)

    # ── IA ──
    def context(self) -> dict:
        ctx = {"date_utc": _iso(self.now()), "indicateurs_journaliers": technicals(self.cfg),
               "frais": {"maker": self.cfg.fees.maker, "taker": self.cfg.fees.taker},
               "blackouts_config": [(_iso(a), _iso(b)) for a, b in self.cfg.risk.blackout],
               "poches": {s: {"reel": r.get("live_allowed"), "eligible": r.get("eligible")}
                          for s, r in self.promo.items()}}
        for mode in ("paper", "live"):
            journal = self.paper_j if mode == "paper" else self.live_j
            if journal is None:
                continue
            ctx[mode] = {
                "positions": [{"poche": t.sleeve, "paire": t.product, "statut": t.status,
                               "entree": t.entry_price or t.entry_limit, "stop": t.stop}
                              for t in journal.active()],
                "dca": {p: {"quantite": h["qty"], "prix_moyen": h["avg_price"]}
                        for p, h in journal.dca_holdings().items()},
                "derniers_trades": [{"paire": t.product, "poche": t.sleeve, "sortie": t.exit_reason,
                                     "pnl": round(t.pnl, 2), "date": t.exit_time}
                                    for t in journal.closed()[-10:]],
            }
        return ctx

    def ai_report(self) -> None:
        if not self.analyst:
            raise AnalystError("analyste IA indisponible (clé ANTHROPIC_API_KEY absente)")
        rep = self.analyst.daily_report(self.context(), now=self.now())
        c = rep["controles"]
        self.event("INFO", "ai", f"rapport quotidien : régime {rep['regime']}, multiplicateur "
                   f"{c['risk_multiplier']}, {len(c['blackouts'])} blackout(s), {rep['cout_usd']:.3f} $")

    # ── commandes du site ──
    def process_commands(self) -> None:
        for cmd in self.control.pending_commands():
            payload = json.loads(cmd["payload"] or "{}")
            try:
                result = self.execute(cmd["kind"], payload)
                self.control.finish_command(cmd["id"], True, result)
                self.event("INFO", "web", f"{cmd['kind']} par {cmd['author']} : {result}")
            except Exception as e:  # noqa: BLE001
                self.control.finish_command(cmd["id"], False, f"{type(e).__name__}: {e}")
                self.event("WARNING", "web", f"{cmd['kind']} par {cmd['author']} en échec : {e}")

    def execute(self, kind: str, p: dict) -> str:
        c, kill = self.control, Path(self.cfg.risk.kill_switch_file)
        if kind in ("pause", "resume"):
            c.set_state("paused", "1" if kind == "pause" else "0")
            return "trading en pause" if kind == "pause" else "trading repris"
        if kind == "kill_on":
            kill.parent.mkdir(parents=True, exist_ok=True)
            kill.write_text(_iso(self.now()))
            return "kill switch activé"
        if kind == "kill_off":
            kill.unlink(missing_ok=True)
            return "kill switch retiré"
        if kind in ("auto_on", "auto_off"):
            c.set_state("auto_promotion", "on" if kind == "auto_on" else "off")
            return f"promotion automatique {'activée' if kind == 'auto_on' else 'coupée'}"
        if kind in ("demote", "undemote"):
            c.set_state(f"demoted:{p['sleeve']}", "1" if kind == "demote" else "0")
            self.update_promotion()
            return f"poche {p['sleeve']} {'rétrogradée en paper' if kind == 'demote' else 'réautorisée'}"
        if kind == "close_trade":
            return self._close_trade(p["mode"], int(p["trade_id"]))
        if kind == "analyze_now":
            self.ai_report()
            return "rapport généré"
        if kind == "ask":
            if not self.analyst:
                raise AnalystError("analyste IA indisponible (clé ANTHROPIC_API_KEY absente)")
            r = self.analyst.answer(p["question"], self.context(), now=self.now())
            return f"réponse enregistrée ({r['cout_usd']:.3f} $)"
        raise ValueError(f"commande inconnue : {kind}")

    def _close_trade(self, mode: str, trade_id: int) -> str:
        journal = self.paper_j if mode == "paper" else self.live_j
        t = journal.trade(trade_id) if journal else None
        if t is None or t.status != "OPEN":
            raise ValueError("position introuvable ou déjà clôturée")
        eng = self.engines[mode].get(t.sleeve)
        if eng is None:
            raise ValueError(f"poche {t.sleeve} inactive en {mode}")
        price = self.prices.get(t.product) or market.last_price(t.product)
        eng.close_now(t, price, "manual")
        return f"{t.product} vendu à {t.exit_price:.4f} (P&L {t.pnl:+.2f} €)"

    # ── boucle ──
    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()

        def ask_stop(signum, _frame):
            if stop.is_set():
                raise KeyboardInterrupt
            log.info("Arrêt demandé — fin de la tâche en cours…")
            stop.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, ask_stop)
            signal.signal(signal.SIGTERM, ask_stop)
        self.event("INFO", "bot", "démarrage (paper ombre"
                   f"{', IA' if self.analyst else ''}) — poches : {', '.join(promotion.enabled_sleeves(self.cfg))}")
        while not stop.is_set():
            self.sched.tick(self.now())
            stop.wait(5)
        self.event("INFO", "bot", "arrêt propre")


def summary_next_runs(runner: Runner) -> list[tuple[str, str]]:
    now = datetime.now(UTC)
    return [(j.name, _iso(runner.sched.next_run(j, now))) for j in runner.sched.jobs]


__all__ = ["Runner", "summary_next_runs"]
