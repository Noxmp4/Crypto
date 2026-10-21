"""Site web : authentification Cloudflare Access, CSRF, file de commandes, rendu."""
import dataclasses
import re
import time
from urllib.parse import unquote

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from daybot.config import Config  # noqa: E402
from daybot.journal import Journal, Trade  # noqa: E402
from daybot.web.app import create_app, safe_url  # noqa: E402
from daybot.web.auth import AccessVerifier  # noqa: E402
from daybot.web.charts import line_chart  # noqa: E402

TEAM, AUD = "equipe.cloudflareaccess.com", "aud-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def _token(**over):
    claims = {"email": "moi@example.com", "aud": AUD, "iss": f"https://{TEAM}",
              "iat": int(time.time()), "exp": int(time.time()) + 600, **over}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, KEY, algorithm="RS256")


@pytest.fixture()
def cfg(tmp_path):
    c = Config(data_dir=str(tmp_path), journal_path=str(tmp_path / "journal.sqlite"))
    c.risk = dataclasses.replace(c.risk, kill_switch_file=str(tmp_path / "STOP"))
    Journal(c.paper_journal_path)
    Journal(c.control_path)
    return c


def _client(cfg):
    return TestClient(create_app(cfg, dev=False, verifier=AccessVerifier(TEAM, AUD, FakeJWKS())))


def test_closed_by_default_without_access_config(cfg):
    c = TestClient(create_app(cfg, dev=False))
    assert c.get("/").status_code == 403
    assert c.get("/healthz").status_code == 200


@pytest.mark.parametrize("token,ok", [
    (lambda: _token(), True),
    (lambda: _token(aud="autre"), False),
    (lambda: _token(iss="https://pirate.example"), False),
    (lambda: _token(exp=int(time.time()) - 10), False),
    (lambda: _token(email=None), False),
    (lambda: "pas-un-jwt", False),
    (lambda: "", False),
])
def test_access_jwt_verification(cfg, token, ok):
    r = _client(cfg).get("/", headers={"Cf-Access-Jwt-Assertion": token()})
    assert (r.status_code == 200) is ok
    if ok:
        assert "moi@example.com" in r.text
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_cookie_is_accepted(cfg):
    c = _client(cfg)
    c.cookies.set("CF_Authorization", _token())
    assert c.get("/sante").status_code == 200


def _csrf(client, hdr):
    html = client.get("/controle", headers=hdr).text
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def test_commands_require_csrf_confirmation_and_same_origin(cfg):
    c, hdr = _client(cfg), {"Cf-Access-Jwt-Assertion": _token()}
    tok = _csrf(c, hdr)
    control = Journal(cfg.control_path)
    assert c.post("/commande", data={"kind": "pause", "confirm": "oui"}, headers=hdr).status_code == 403
    r = c.post("/commande", data={"kind": "pause", "csrf": tok}, headers=hdr, follow_redirects=False)
    assert r.status_code == 303 and "Cochez" in unquote(r.headers["location"])
    assert control.pending_commands() == []
    r = c.post("/commande", data={"kind": "pause", "csrf": tok, "confirm": "oui"}, headers=hdr,
               follow_redirects=False)
    assert r.status_code == 303
    cmd = control.pending_commands()[0]
    assert cmd["kind"] == "pause" and cmd["author"] == "moi@example.com"
    bad = {**hdr, "Origin": "https://pirate.example"}
    assert c.post("/commande", data={"kind": "pause", "csrf": tok, "confirm": "oui"},
                  headers=bad).status_code == 403
    assert c.post("/commande", data={"kind": "rm -rf", "csrf": tok, "confirm": "oui"},
                  headers=hdr).status_code == 400
    # un jeton CSRF est lié à l'utilisateur
    other = {"Cf-Access-Jwt-Assertion": _token(email="autre@example.com")}
    assert c.post("/commande", data={"kind": "pause", "csrf": tok, "confirm": "oui"},
                  headers=other).status_code == 403


def test_payload_validation(cfg):
    c, hdr = _client(cfg), {"Cf-Access-Jwt-Assertion": _token()}
    tok = _csrf(c, hdr)
    base = {"csrf": tok, "confirm": "oui"}
    assert c.post("/commande", data={**base, "kind": "demote", "sleeve": "inconnue"},
                  headers=hdr).status_code == 400
    assert c.post("/commande", data={**base, "kind": "close_trade", "mode": "live",
                                     "trade_id": "1; DROP"}, headers=hdr).status_code == 400
    c.post("/commande", data={**base, "kind": "demote", "sleeve": "trend"}, headers=hdr)
    c.post("/commande", data={"csrf": tok, "kind": "ask", "question": "  Et le BTC ?  "}, headers=hdr)
    cmds = {r["kind"]: r["payload"] for r in Journal(cfg.control_path).pending_commands()}
    assert cmds["demote"] == '{"sleeve": "trend"}' and cmds["ask"] == '{"question": "Et le BTC ?"}'
    r = c.post("/commande", data={**base, "kind": "pause", "back": "//pirate.example"}, headers=hdr,
               follow_redirects=False)
    assert r.headers["location"].startswith("/?")                   # pas de redirection ouverte


def test_pages_render_and_escape(cfg):
    paper = Journal(cfg.paper_journal_path)
    paper.save(Trade("BTC-EUR", "x", 1, 0.9, 0, 1, status="OPEN", entry_price=1, sleeve="trend"))
    for h in range(3):
        paper.snapshot("total", 1000 + h, f"2026-10-20T0{h}:00:00Z")
    Journal(cfg.control_path).event("ERROR", "test", "<script>alert(1)</script>")
    c, hdr = _client(cfg), {"Cf-Access-Jwt-Assertion": _token()}
    for path in ("/", "/analyse", "/trades", "/strategies", "/controle", "/sante"):
        assert c.get(path, headers=hdr).status_code == 200, path
    sante = c.get("/sante", headers=hdr).text
    assert "<script>alert(1)</script>" not in sante and "&lt;script&gt;" in sante
    csv = c.get("/export/paper.csv", headers=hdr)
    assert csv.status_code == 200 and csv.text.startswith("date_cession_utc")
    assert c.get("/export/live.csv", headers=hdr).status_code == 404


def test_safe_url_and_chart_payload():
    assert safe_url("javascript:alert(1)") == "#" and safe_url("https://a.b/c") == "https://a.b/c"
    html = str(line_chart({"</script><img src=x>": [("2026-10-20T00:00:00Z", 1.0),
                                                     ("2026-10-20T01:00:00Z", 2.0)]}, "t"))
    data = html.split('class="chart-data">')[1].split("</script>")[0]
    assert "<" not in data and "\\u003c/script\\u003e" in data
    assert "<img src=x>" not in html
