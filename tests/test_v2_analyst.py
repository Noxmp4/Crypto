"""Analyste IA : appels simulés (aucun appel réseau), garde-fous, budget."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from daybot.analyst import (Analyst, AnalystError, BudgetExceeded, active_controls,
                            apply_guardrails, usage_cost)
from daybot.config import Config
from daybot.journal import Journal

NOW = datetime(2026, 10, 27, 7, 30, tzinfo=timezone.utc)


def _usage(i=1000, o=500, searches=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=0,
              cache_creation_input_tokens=0, server_tool_use=NS(web_search_requests=searches))


def _resp(content, stop="end_turn", usage=None, model="claude-opus-5-5"):
    return NS(content=content, stop_reason=stop, usage=usage or _usage(), model=model)


def _report(**over):
    r = {"resume": "Marché prudent avant le FOMC.", "regime": "neutre",
         "actifs": [{"paire": "BTC-EUR", "tendance": "neutre", "supports": [70000],
                     "resistances": [75000], "commentaire": "range"}],
         "risques": ["FOMC"], "scenarios": [{"nom": "neutre", "declencheurs": "range",
                                            "implications": "attendre"}],
         "conseils": [{"texte": "Garder le DCA.", "horizon": "semaine"}],
         "risk_multiplier": "0.5", "justification_risque": "FOMC demain",
         "blackouts": [
             {"debut_utc": "2026-10-28T16:00:00Z", "fin_utc": "2026-10-29T02:00:00Z", "motif": "FOMC"},
             {"debut_utc": "2026-10-28T00:00:00Z", "fin_utc": "2026-11-02T00:00:00Z", "motif": "trop long"},
             {"debut_utc": "2026-10-01T00:00:00Z", "fin_utc": "2026-10-02T00:00:00Z", "motif": "passé"}],
         "sources": [{"titre": "Fed", "url": "https://example.com", "date": "2026-10-26"}]}
    r.update(over)
    return r


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _analyst(responses, **ai):
    cfg = Config()
    for k, v in ai.items():
        setattr(cfg.ai, k, v)
    control = Journal()
    client = FakeClient(responses)
    return Analyst(cfg, control, client), control, client


def test_daily_report_two_calls_with_search_and_guardrails():
    search_result = NS(type="web_search_tool_result", content=[
        NS(title="Fed preview", url="https://example.com/fed", page_age="2026-10-26")])
    a, control, client = _analyst([
        _resp([NS(type="text", text="notes 1"), search_result], stop="pause_turn",
              usage=_usage(searches=2)),
        _resp([NS(type="text", text="notes 2")]),
        _resp([NS(type="text", text=json.dumps(_report()))]),
    ])
    rep = a.daily_report({"btc": 1}, now=NOW)
    research, resumed, synthesis = client.calls
    assert research["tools"][0]["type"] == "web_search_20260209"
    assert research["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in research["betas"]
    assert research["thinking"] == {"type": "adaptive"} and research["model"] == "claude-opus-5-5"
    assert research["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert resumed["messages"][-1]["role"] == "assistant"          # reprise après pause_turn
    assert synthesis["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in synthesis
    assert "Fed preview" in synthesis["messages"][0]["content"]
    ctl = rep["controles"]
    assert ctl["risk_multiplier"] == 0.5
    assert [b["motif"] for b in ctl["blackouts"]] == ["FOMC"]       # trop long / passé rejetés
    assert control.get_json("ai_controls") == ctl
    assert control.analyses("daily")[0]["kind"] == "daily"
    assert rep["cout_usd"] == pytest.approx(3 * (1000 * 4 + 500 * 20) / 1e6 + 2 * 0.01)
    assert a.spent_this_month(NOW) == pytest.approx(rep["cout_usd"])


def test_guardrails_never_increase_risk():
    g = apply_guardrails(_report(risk_multiplier="2"), NOW)
    assert g["risk_multiplier"] == 1.0
    many = [{"debut_utc": (NOW + timedelta(hours=h)).isoformat(),
             "fin_utc": (NOW + timedelta(hours=h + 2)).isoformat(), "motif": str(h)} for h in range(6)]
    assert len(apply_guardrails(_report(blackouts=many), NOW)["blackouts"]) == 3


def test_active_controls_expire():
    control = Journal()
    control.set_json("ai_controls", apply_guardrails(_report(risk_multiplier="0"), NOW))
    mult, bl = active_controls(control, NOW + timedelta(hours=1))
    assert mult == 0 and len(bl) == 1
    mult, bl = active_controls(control, NOW + timedelta(days=3))
    assert mult == 1.0 and bl == []


def test_budget_blocks_calls():
    a, control, client = _analyst([], monthly_budget_usd=1.0)
    control.record_ai_usage("x", "claude-opus-5-5", {}, 1.5)
    with pytest.raises(BudgetExceeded):
        a.daily_report({}, now=NOW)
    assert client.calls == []


def test_refusal_and_invalid_json_raise():
    a, _, _ = _analyst([_resp([], stop="refusal")])
    with pytest.raises(AnalystError, match="refusée"):
        a.daily_report({}, now=NOW)
    a, control, _ = _analyst([_resp([NS(type="text", text="n")]),
                              _resp([NS(type="text", text="{pas du json")])])
    with pytest.raises(AnalystError, match="JSON"):
        a.daily_report({}, now=NOW)
    assert control.get_json("ai_controls") is None                  # rien appliqué


def test_questions_are_limited_per_day():
    a, control, client = _analyst([_resp([NS(type="text", text="Réponse")])] * 2,
                                  max_questions_per_day=1)
    r = a.answer("Faut-il renforcer le DCA ?", {}, now=NOW)
    assert r["reponse"] == "Réponse" and "<question_du_proprietaire>" in client.calls[0]["messages"][0]["content"]
    with pytest.raises(AnalystError, match="limite"):
        a.answer("Encore ?", {}, now=NOW)


def test_unknown_fallback_model_billed_at_highest_price():
    cost, _ = usage_cost("claude-inconnu", _usage(1_000_000, 0))
    assert cost == pytest.approx(10.0)
