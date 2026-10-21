"""Application FastAPI du tableau de bord.

Le site ne parle jamais à Coinbase ni à Anthropic et ne détient aucune de leurs clés :
il lit les journaux SQLite (lecture seule) et écrit des commandes dans la table
`commands` du fichier de contrôle, que le bot exécute à son rythme.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import promotion
from ..config import Config
from ..journal import Journal
from .auth import AccessVerifier, AuthError, csrf_ok, csrf_token
from .charts import LABELS, eur, line_chart

HERE = Path(__file__).parent
SLEEVE_NAMES = {"dca": "Cœur DCA", "trend": "Tendance", "intraday": "Intraday"}
CONFIRMED = {"pause", "resume", "kill_on", "kill_off", "auto_on", "auto_off", "demote",
             "undemote", "close_trade"}
ALLOWED = CONFIRMED | {"analyze_now", "ask"}


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def safe_url(u: str) -> str:
    """Liens issus de l'IA / du web : http(s) uniquement (jamais javascript:)."""
    try:
        return u if urlparse(u).scheme in ("http", "https") else "#"
    except ValueError:
        return "#"


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


class Data:
    """Accès aux fichiers du bot, ouverts à chaque requête."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def journal(self, mode: str) -> Journal | None:
        path = self.cfg.paper_journal_path if mode == "paper" else self.cfg.journal_path
        return Journal(path, readonly=True) if os.path.exists(path) else None

    def control(self, write: bool = False) -> Journal | None:
        path = self.cfg.control_path
        if write:
            return Journal(path)
        return Journal(path, readonly=True) if os.path.exists(path) else None


def mode_summary(cfg: Config, j: Journal | None, prices: dict) -> dict | None:
    if j is None:
        return None
    curve = {s: j.equity_curve(s) for s in ("total", "trend", "dca")}
    last = {s: (pts[-1][1] if pts else None) for s, pts in curve.items()}
    positions = []
    for t in j.active():
        px = prices.get(t.product)
        latent = (px - t.entry_price) * t.base_size - t.entry_fee if (px and t.status == "OPEN") else None
        positions.append({"t": t, "price": px, "latent": latent,
                          "sleeve": SLEEVE_NAMES.get(t.sleeve, t.sleeve)})
    holdings = []
    for p, h in j.dca_holdings().items():
        px = prices.get(p)
        value = h["qty"] * px if px else None
        holdings.append({"product": p, **h, "price": px, "value": value,
                         "gain": (value - h["cost"]) if value is not None else None})
    realized = {s: sum(t.pnl for t in j.closed(s)) for s in ("trend", "intraday")}
    return {"curve": curve, "last": last, "positions": positions, "holdings": holdings,
            "realized": realized, "closed_count": len(j.closed())}


def create_app(cfg: Config, dev: bool | None = None, verifier: AccessVerifier | None = None) -> FastAPI:
    dev = os.getenv("DAYBOT_WEB_DEV") == "1" if dev is None else dev
    data = Data(cfg)
    app = FastAPI(title="daybot", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(eur=eur, safe_url=safe_url, paragraphs=paragraphs)
    templates.env.globals.update(SLEEVE_NAMES=SLEEVE_NAMES, LABELS=LABELS)
    if verifier is None and cfg.web.cf_team_domain and cfg.web.cf_aud:
        verifier = AccessVerifier(cfg.web.cf_team_domain, cfg.web.cf_aud)

    def web_secret() -> str:
        c = data.control(write=True)
        s = c.get_state("web_secret")
        if not s:
            s = secrets.token_hex(32)
            c.set_state("web_secret", s)
        return s

    @app.middleware("http")
    async def security(request: Request, call_next):
        path = request.url.path
        if path in ("/healthz", "/favicon.ico") or path.startswith("/static/"):
            response = await call_next(request)
        else:
            try:
                if dev:
                    request.state.email = "dev@local"
                elif verifier is None:
                    raise AuthError("Cloudflare Access n'est pas configuré ([web] dans config.toml)")
                else:
                    token = (request.headers.get("cf-access-jwt-assertion")
                             or request.cookies.get("CF_Authorization", ""))
                    request.state.email = verifier.verify(token)["email"]
            except AuthError as e:
                response = HTMLResponse(f"<h1>Accès refusé</h1><p>{_esc(str(e))}</p>", status_code=403)
            else:
                response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        control = data.control()
        hb = _parse(control.get_state("heartbeat")) if control else None
        age = (datetime.now(timezone.utc) - hb).total_seconds() if hb else None
        ctx.update(
            email=request.state.email, csrf=csrf_token(web_secret(), request.state.email),
            nav=name, flash=request.query_params.get("msg", ""),
            bot_ok=age is not None and age < 180, bot_age=age,
            paused=control.get_state("paused") == "1" if control else False,
            killed=os.path.exists(cfg.risk.kill_switch_file), dev=dev)
        return templates.TemplateResponse(request, f"{name}.html", ctx)

    def common() -> tuple[Journal | None, dict, dict]:
        control = data.control()
        prices = (control.get_json("prices") or {}).get("prices", {}) if control else {}
        promo = (control.get_json("promotion") or {}) if control else {}
        return control, prices, promo

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"ok": True})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/favicon.svg", 301)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        control, prices, promo = common()
        paper = mode_summary(cfg, data.journal("paper"), prices)
        live = mode_summary(cfg, data.journal("live"), prices)
        charts = {}
        for mode, summ in (("paper", paper), ("live", live)):
            if summ:
                title = "Capital simulé (paper) par poche" if mode == "paper" else "Capital réel par poche"
                charts[mode] = line_chart(summ["curve"], title)
        report = control.analyses("daily", 1) if control else []
        report = json.loads(report[0]["result"]) if report else None
        spent = control.ai_spent(_month_start()) if control else 0.0
        return page(request, "dashboard", paper=paper, live=live, charts=charts, prices=prices,
                    promo=promo, report=report, spent=spent, cfg=cfg)

    @app.get("/analyse", response_class=HTMLResponse)
    def analyse(request: Request):
        control = data.control()
        daily = control.analyses("daily", 15) if control else []
        questions = control.analyses("question", 15) if control else []
        reports = [{"created": r["created"], "cost": r["cost_usd"], **json.loads(r["result"])}
                   for r in daily]
        answers = [{"created": r["created"], "question": r["question"], "cost": r["cost_usd"],
                    **json.loads(r["result"])} for r in questions]
        pending = [r for r in (control.commands(20) if control else [])
                   if r["kind"] in ("ask", "analyze_now") and r["status"] == "NEW"]
        spent = control.ai_spent(_month_start()) if control else 0.0
        return page(request, "analyse", reports=reports, answers=answers, pending=pending,
                    spent=spent, cfg=cfg)

    @app.get("/trades", response_class=HTMLResponse)
    def trades(request: Request):
        out = {}
        for mode in ("paper", "live"):
            j = data.journal(mode)
            if j:
                out[mode] = {"trades": j.recent_trades(100), "lots": list(reversed(j.lots()))[:100]}
        return page(request, "trades", modes=out)

    @app.get("/strategies", response_class=HTMLResponse)
    def strategies(request: Request):
        control, _prices, promo = common()
        research = control.get_json("research") if control else None
        return page(request, "strategies", research=research, promo=promo, cfg=cfg)

    @app.get("/controle", response_class=HTMLResponse)
    def controle(request: Request):
        control, prices, promo = common()
        open_trades = []
        for mode in ("paper", "live"):
            j = data.journal(mode)
            if j:
                open_trades += [(mode, t) for t in j.active() if t.status == "OPEN"]
        return page(request, "controle", promo=promo, open_trades=open_trades,
                    commands=control.commands(30) if control else [],
                    auto=(control.get_state("auto_promotion", "on") != "off") if control else True,
                    sleeves=promotion.enabled_sleeves(cfg), cfg=cfg)

    @app.get("/sante", response_class=HTMLResponse)
    def sante(request: Request):
        control = data.control()
        events = control.events(80) if control else []
        usage = control.db.execute(
            "SELECT kind, COUNT(*) n, SUM(cost_usd) c FROM ai_usage WHERE time>=? GROUP BY kind",
            (_month_start(),)).fetchall() if control else []
        return page(request, "sante", events=events, usage=usage,
                    spent=control.ai_spent(_month_start()) if control else 0.0,
                    fees=(control.get_json("fees") or {}) if control else {}, cfg=cfg)

    @app.post("/commande")
    def commande(request: Request, kind: str = Form(...), csrf: str = Form(""),
                 confirm: str = Form(""), sleeve: str = Form(""), mode: str = Form(""),
                 trade_id: str = Form(""), question: str = Form(""), back: str = Form("/")):
        email = request.state.email
        if not csrf_ok(web_secret(), email, csrf):
            raise HTTPException(403, "jeton CSRF invalide")
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname != request.url.hostname:
            raise HTTPException(403, "origine refusée")
        back = back if back.startswith("/") and not back.startswith("//") else "/"
        if kind not in ALLOWED:
            raise HTTPException(400, "commande inconnue")
        if kind in CONFIRMED and confirm != "oui":
            return RedirectResponse(f"{back}?msg={quote('Cochez la case de confirmation.')}", 303)
        payload: dict = {}
        if kind in ("demote", "undemote"):
            if sleeve not in promotion.enabled_sleeves(cfg):
                raise HTTPException(400, "poche inconnue")
            payload["sleeve"] = sleeve
        if kind == "close_trade":
            if mode not in ("paper", "live") or not trade_id.isdigit():
                raise HTTPException(400, "position invalide")
            payload.update(mode=mode, trade_id=int(trade_id))
        if kind == "ask":
            question = question.strip()
            if not question or len(question) > 2000:
                return RedirectResponse(f"{back}?msg={quote('Question vide ou trop longue.')}", 303)
            payload["question"] = question
        data.control(write=True).add_command(kind, payload, email)
        msg = "Commande envoyée : le bot l'exécute dans la minute."
        return RedirectResponse(f"{back}?msg={quote(msg)}", 303)

    @app.get("/export/{mode}.csv")
    def export(mode: str):
        j = data.journal(mode)
        if mode not in ("paper", "live") or j is None:
            raise HTTPException(404, "journal absent")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cessions.csv")
            j.export_tax_csv(path)
            body = Path(path).read_bytes()
        return Response(body, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="cessions_{mode}.csv"'})

    return app


def _esc(s: str) -> str:
    from markupsafe import escape
    return str(escape(s))


def _month_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
