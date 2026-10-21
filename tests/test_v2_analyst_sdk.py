"""Intégration avec le vrai SDK `anthropic`, contre un serveur HTTP local qui imite l'API.

Vérifie que nos paramètres sont acceptés et sérialisés par le SDK (outils, sortie
structurée, repli, en-tête beta) et que les réponses typées sont bien lues — sans
aucun appel réseau ni clé réelle.
"""
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

anthropic = pytest.importorskip("anthropic")

from daybot.analyst import Analyst  # noqa: E402
from daybot.config import Config  # noqa: E402
from daybot.journal import Journal  # noqa: E402

REPORT = {"resume": "ok", "regime": "neutre", "actifs": [], "risques": [], "scenarios": [],
          "conseils": [], "risk_multiplier": "1", "justification_risque": "", "blackouts": [],
          "sources": []}


def _msg(content, stop="end_turn", searches=0):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "server_tool_use": {"web_search_requests": searches}}}


RESPONSES = [
    _msg([{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
           "input": {"query": "bitcoin FOMC"}},
          {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
              {"type": "web_search_result", "url": "https://example.com/fed", "title": "Fed",
               "encrypted_content": "abc", "page_age": "26 octobre 2026"}]}],
         stop="pause_turn", searches=1),
    _msg([{"type": "text", "text": "Notes : FOMC le 28/10."}]),
    _msg([{"type": "text", "text": json.dumps(REPORT)}]),
]


@pytest.fixture()
def fake_api():
    seen = []
    queue = list(RESPONSES)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
            out = json.dumps(queue.pop(0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_real_sdk_round_trip(fake_api):
    url, seen = fake_api
    client = anthropic.Anthropic(api_key="test", base_url=url, max_retries=0)
    a = Analyst(Config(), Journal(), client)
    rep = a.daily_report({"x": 1}, now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert rep["controles"]["risk_multiplier"] == 1.0
    first, resumed, synthesis = seen
    assert first["path"].startswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in first["headers"].get("anthropic-beta", "")
    assert first["body"]["fallbacks"] == "default"
    assert first["body"]["tools"][0] == {"type": "web_search_20260209", "name": "web_search",
                                         "max_uses": 5}
    assert first["body"]["output_config"] == {"effort": "high"}
    # reprise : le contenu typé renvoyé par le SDK est resérialisé tel quel
    assert resumed["body"]["messages"][-1]["role"] == "assistant"
    assert resumed["body"]["messages"][-1]["content"][0]["type"] == "server_tool_use"
    assert synthesis["body"]["output_config"]["format"]["type"] == "json_schema"
    assert "https://example.com/fed" in synthesis["body"]["messages"][0]["content"]
