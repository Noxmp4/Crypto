"""Passage paper → réel, poche par poche.

Une poche passe en réel automatiquement quand TOUTES ces conditions sont réunies :
  1. accord donné au déploiement : DAYBOT_LIVE_CONFIRM=OUI, une clé API Coinbase
     disponible et un plafond `max_live_capital_eur` > 0 ;
  2. assez de jours de paper sans erreur d'exécution ;
  3. pour les poches à signal (tendance) : validation hors échantillon suffisante ;
  4. promotion automatique active et poche non rétrogradée depuis le site.
Une poche promue garde le droit d'ouvrir des positions tant que 1, 3 et 4 restent vrais.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from . import credentials
from .config import Config
from .journal import Journal


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def has_live_key() -> bool:
    try:
        credentials.credentials()
        return True
    except Exception:  # noqa: BLE001
        return False


def enabled_sleeves(cfg: Config) -> list[str]:
    out = []
    if cfg.dca.enabled:
        out.append("dca")
    if cfg.trend.enabled:
        out.append("trend")
    if cfg.intraday_enabled:
        out.append("intraday")
    return out


def _check(label: str, ok: bool, detail: str) -> dict:
    return {"label": label, "ok": bool(ok), "detail": detail}


def consent_checks(cfg: Config, key_ok: bool) -> list[dict]:
    return [
        _check("Accord au déploiement (DAYBOT_LIVE_CONFIRM=OUI)",
               os.getenv("DAYBOT_LIVE_CONFIRM") == "OUI", "variable d'environnement du VPS"),
        _check("Clé API Coinbase disponible", key_ok, "permissions View + Trade"),
        _check("Plafond d'euros réels fixé", cfg.promotion.max_live_capital_eur > 0,
               f"{cfg.promotion.max_live_capital_eur:.0f} € (max_live_capital_eur)"),
    ]


def research_checks(cfg: Config, sleeve: str, control: Journal) -> list[dict]:
    if sleeve == "dca":
        return []
    if sleeve == "intraday":
        return [_check("Validation hors échantillon", False,
                       "non disponible pour l'intraday (perdant en backtest)")]
    pr = cfg.promotion
    oos = ((control.get_json("research") or {}).get("tendance") or {}).get("hors_echantillon")
    if not oos:
        return [_check("Validation hors échantillon", False, "backtest pas encore calculé")]
    pf = oos.get("profit_factor") or 0.0
    dd = abs(oos.get("max_drawdown_pct") or 0.0) / 100
    return [
        _check(f"Profit factor hors échantillon ≥ {pr.trend_min_oos_profit_factor}",
               pf >= pr.trend_min_oos_profit_factor, f"{pf:.2f} ({oos.get('periode', '')})"),
        _check(f"Drawdown max hors échantillon ≤ {pr.trend_max_oos_drawdown:.0%}",
               dd <= pr.trend_max_oos_drawdown, f"{dd:.1%}"),
    ]


def evaluate(cfg: Config, paper: Journal, control: Journal, now: datetime | None = None,
             key_ok: bool | None = None) -> dict[str, dict]:
    now = now or datetime.now(timezone.utc)
    key_ok = has_live_key() if key_ok is None else key_ok
    pr = cfg.promotion
    auto = pr.auto and control.get_state("auto_promotion", "on") != "off"
    consent = consent_checks(cfg, key_ok)
    out = {}
    for sleeve in enabled_sleeves(cfg):
        min_days = {"dca": pr.dca_min_days, "trend": pr.trend_min_days,
                    "intraday": pr.intraday_min_days}[sleeve]
        started = paper.get_state(f"paper_started:{sleeve}")
        days = (now - datetime.fromisoformat(started.replace("Z", "+00:00"))).days if started else 0
        errors = control.count_events("ERROR", f"paper:{sleeve}", _iso(now - timedelta(days=min_days)))
        checks = [
            _check(f"Au moins {min_days} jours de paper", days >= min_days, f"{days} j"),
            _check("Aucune erreur d'exécution en paper", errors == 0,
                   f"{errors} erreur(s) sur {min_days} j"),
            *research_checks(cfg, sleeve, control),
            *consent,
        ]
        demoted = control.get_state(f"demoted:{sleeve}") == "1"
        live_since = control.get_state(f"live_since:{sleeve}")
        out[sleeve] = {
            "eligible": all(c["ok"] for c in checks) and auto and not demoted,
            "checks": checks,
            "auto": auto,
            "demoted": demoted,
            "live_since": live_since,
            # Une poche déjà promue peut continuer tant que l'accord, la validation
            # et l'absence de rétrogradation tiennent (les jours de paper sont acquis).
            "live_allowed": bool(live_since) and not demoted and all(
                c["ok"] for c in [*consent, *research_checks(cfg, sleeve, control)]),
        }
    return out


def deployed_eur(live: Journal) -> float:
    """Euros réels engagés par le bot : positions ouvertes / en attente + achats DCA."""
    total = 0.0
    for t in live.active():
        px = t.entry_price or t.entry_limit
        total += px * t.base_size + t.entry_fee
    for lot in live.lots():
        if lot.status == "FILLED":
            total += lot.cost
        elif lot.status == "PENDING":
            total += lot.quote_amount
    return total


def deployable_eur(cfg: Config, live: Journal) -> float:
    return max(0.0, cfg.promotion.max_live_capital_eur - deployed_eur(live))
