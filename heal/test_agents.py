"""LLM agent tests with a fake agent.llm.ask_json. No Docker, no real LLM, no data/ writes:
run `python -m pytest heal -q`."""
import inspect
import json

import pytest

from agent import llm
from heal import agents, graph, ports, run as cli

DEP = graph._load("mock_depmap.json")
BAS = graph._load("mock_baseline.json")
EXP_KEYS = {"id", "target", "fault", "params", "reason", "hypothesis", "observations", "experiment", "rejection"}
BRIEF_KEYS = {"error_rate", "p95_ms", "failing_endpoints", "unhealthy", "healthy_count", "containers_not_running",
              "cpu_busy_pct", "netem", "logs"}
CHAOS_OK = {"target": "redis", "fault": "kill", "params": {"duration_s": 30}, "reason": "the user asked to kill redis",
            "hypothesis": "cart fails while redis is down; recovers after restart"}
DIAG_OK = {"suspect_service": "redis", "fault_guess": "kill", "affected_services": ["redis", "cart"],
           "evidence": ["redis container exited", "error_rate 0.004 -> 0.62"], "confidence": 0.9}
REM_OK = {"actions": [{"action": "start_service", "target": "redis"}], "reason": "redis exited"}


class FakeLLM:
    """Stands in for llm.ask_json: runs `check` on each canned reply like the real retry loop (a ValueError is
    recorded as the feedback the model would get), raises once a call has used its tries or the replies run out."""

    def __init__(self, *replies):
        self.replies, self.calls, self.errors = list(replies), [], []

    def __call__(self, sys_prompt, usr_prompt, tries=3, schema=None, check=None, cap=None):
        self.calls.append({"sys": sys_prompt, "usr": usr_prompt, "cap": cap})
        for _ in range(tries):
            if not self.replies:
                break
            raw = self.replies.pop(0)
            try:
                return check(raw) if check else raw
            except (ValueError, KeyError, TypeError) as err:
                self.errors.append(str(err))
        raise ValueError(f"no valid JSON after {tries} tries: {self.errors[-1:]}")

    def payload(self, idx=-1):
        """The JSON data of call idx (the user prompt is '<task>\\n<json>')."""
        return json.loads(self.calls[idx]["usr"].split("\n", 1)[1])


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setenv("CHAOS_NO_LLM", "1")  # anything not faked uses the fallback, never a real LLM


@pytest.fixture
def fake(monkeypatch):
    def use(*replies):
        fkl = FakeLLM(*replies)
        monkeypatch.setattr(llm, "ask_json", fkl)
        return fkl
    return use


def keys(obj):
    """Every dict key in obj, recursively."""
    if isinstance(obj, dict):
        return set(obj) | set().union(*(keys(val) for val in obj.values()))
    if isinstance(obj, list):
        return set().union(set(), *(keys(val) for val in obj))
    return set()


# ---- chaos agent ----
def test_explicit_prompt_is_honored(fake):
    fkl = fake({**CHAOS_OK, "target": "mongodb", "fault": "pause"}, CHAOS_OK)
    exp, met = agents.plan_experiment("kill redis", [], DEP)
    assert (exp["target"], exp["fault"], met["source"]) == ("redis", "kill", "llm")
    assert "redis" in fkl.errors[0]  # the mismatch was fed back
    assert fkl.payload()["prompt_names"] == {"services": ["redis"], "fault": "kill"}


@pytest.mark.parametrize("prm,tgt,fal", [("kill redis", ["redis"], "kill"), ("please pause mysql for 20s", ["mysql"], "pause"),
                                         ("slow down the catalogue", ["catalogue"], "latency"),
                                         ("stress cart cpu", ["cart"], "cpu_stress"), ("break something", [], None)])
def test_explicit_parses_prompt(prm, tgt, fal):
    assert agents.explicit(prm, DEP) == {"named": tgt, "fault": fal}


def test_vague_prompt_may_pick_top_risk_and_says_so(fake):
    rsk = ports.review(DEP)["risks"]
    fake({**CHAOS_OK, "reason": "carts live only in redis"})
    exp, met = agents.plan_experiment("find a weakness", rsk, DEP)
    assert met["source"] == "llm" and "top code-review risk" in exp["reason"] and "cart/server.js" in exp["reason"]


def test_unsafe_plan_is_rejected_and_retried(fake):
    fkl = fake({**CHAOS_OK, "params": {"duration_s": 90}}, {**CHAOS_OK, "fault": "rm_rf"}, CHAOS_OK)
    exp, met = agents.plan_experiment("kill redis", [], DEP)
    assert met["source"] == "llm" and exp["params"] == {"duration_s": 30}
    assert "safety guard" in fkl.errors[0] and "duration 90s" in fkl.errors[0]
    assert "rm_rf" in fkl.errors[1] and len(fkl.errors) == 2


def test_chaos_falls_back_after_invalid_retries(fake):
    fake(*[{**CHAOS_OK, "params": {"duration_s": 600}}] * 3)
    exp, met = agents.plan_experiment("pause mysql", [], DEP)
    assert met["source"] == "fallback" and "no valid JSON" in met["why"]
    assert (exp["target"], exp["fault"]) == ("mysql", "pause")  # the fallback honours the prompt too


def test_chaos_fallback_without_llm():
    exp, met = agents.plan_experiment("kill rabbitmq", [], DEP)
    assert met == {"source": "fallback", "why": "RuntimeError: LLM disabled by CHAOS_NO_LLM"}
    assert (exp["target"], exp["fault"]) == ("rabbitmq", "kill") and "kill rabbitmq" in exp["reason"]
    exp, _ = agents.plan_experiment("p", ports.review(DEP)["risks"], DEP)
    assert (exp["target"], exp["fault"]) == ("redis", "kill") and "top code-review risk" in exp["reason"]


# ---- diagnosis agent (blind) ----
SECRET = {"id": "exp-SECRET-1", "target": "redis", "fault": "kill", "params": {"duration_s": 30},
          "reason": "SECRET-REASON", "hypothesis": "SECRET-HYP", "rejection": None}


def blind_state(fal="kill"):
    exp = {**SECRET, "fault": fal}
    sym = {"id": exp["id"], "target": "redis", "fault": fal, "error_rate": 0.62, "p95_ms": 2400,
           "observations": ["fault applied: SECRET-INFO"], "measured": graph._stub_measured(exp, BAS)}
    return {"experiment": exp, "symptoms": sym, "baseline": BAS, "depmap": DEP, "app_model": {"risks": []}}


def test_diagnosis_payload_has_no_experiment_fields(fake):
    fkl = fake(DIAG_OK)
    out = graph.diagnosis_agent(blind_state(), {})
    dat = fkl.payload()
    assert set(dat) == {"symptoms", "services"} and set(dat["symptoms"]) == BRIEF_KEYS
    assert not keys(dat["symptoms"]) & EXP_KEYS
    assert not {"id", "fault", "params", "reason", "hypothesis", "observations"} & keys(dat)
    for sec in ("SECRET", "exp-SECRET-1", "fault applied"):
        assert sec not in fkl.calls[0]["usr"]
    assert out["diagnosis_score"] == {"correct": True, "service": True, "fault": True}
    assert out["agents"]["diagnosis_agent"] == [{"source": "llm", "why": None}]


def test_diagnose_cannot_receive_the_experiment():
    assert list(inspect.signature(agents.diagnose).parameters) == ["brf", "dep", "cap"]


def test_wrong_diagnosis_is_scored_wrong(fake):
    fake({**DIAG_OK, "suspect_service": "cart", "fault_guess": "pause"})
    out = graph.diagnosis_agent(blind_state(), {})
    assert out["diagnosis_score"] == {"correct": False, "service": False, "fault": False}


def test_invalid_diagnosis_is_fed_back(fake):
    fkl = fake({**DIAG_OK, "suspect_service": "postgres"}, {**DIAG_OK, "evidence": []}, DIAG_OK)
    dx, met = agents.diagnose(agents.brief(blind_state()["symptoms"], BAS), DEP)
    assert met["source"] == "llm" and dx["suspect_service"] == "redis"
    assert "postgres" in fkl.errors[0] and "evidence" in fkl.errors[1]


@pytest.mark.parametrize("fal", ["kill", "pause", "latency", "cpu_stress"])
def test_rule_diagnosis_is_blind_and_right(fal):
    brf = agents.brief(blind_state(fal)["symptoms"], BAS)
    dx, met = agents.diagnose(brf, DEP)
    assert met["source"] == "fallback"
    assert (dx["suspect_service"], dx["fault_guess"]) == ("redis", fal) and dx["evidence"]


def test_rule_diagnosis_unknown_without_signal():
    brf = agents.brief({"error_rate": 0.0, "p95_ms": 100, "measured": {"health": BAS["health"]}}, BAS)
    assert agents.rule_diagnosis(brf)["fault_guess"] == "unknown" and agents.rule_diagnosis(brf)["suspect_service"] is None


def test_brief_caps_logs():
    sym = blind_state()["symptoms"]
    sym["measured"]["logs"] = {"redis": [f"line {idx} " + "x" * 500 for idx in range(40)], "cart": ["healthy svc"]}
    brf = agents.brief(sym, BAS)
    assert list(brf["logs"]) == ["redis"] and len(brf["logs"]["redis"]) == 15
    assert all(len(lin) <= ports.LOG_W for lin in brf["logs"]["redis"])


def test_heal_deadline_near_skips_the_llm(fake):
    fkl = fake(DIAG_OK)
    _, met = agents.diagnose(agents.brief(blind_state()["symptoms"], BAS), DEP, cap=1)
    assert met["source"] == "fallback" and "deadline" in met["why"] and not fkl.calls


# ---- remediation agent ----
def test_remediation_rejects_non_catalog_actions(fake):
    fkl = fake({"actions": [{"action": "redis_aof", "target": "redis"}], "reason": "persist"},
               {"actions": [{"action": "restart_web", "target": "web"}, {"action": "start_service", "target": "redis"}],
                "reason": "start it"})
    pln, met = agents.remediate(DIAG_OK, DEP)
    assert "not a heal action" in fkl.errors[0] and met["source"] == "llm"
    assert pln["actions"] == [{"action": "start_service", "target": "redis"},
                              {"action": "restart_web", "target": "web"}]  # restart_web moved last automatically


@pytest.mark.parametrize("bad,msg", [({"actions": []}, "non-empty"),
                                     ({"actions": [{"action": "start_service", "target": "postgres"}]}, "postgres"),
                                     ({"actions": [{"action": "restart_web", "target": "web"}]}, "besides restart_web")])
def test_check_remediation_rejects(bad, msg):
    with pytest.raises(ValueError, match=msg):
        agents.check_remediation(bad, DEP)


def test_remediation_fallbacks():
    pln, met = agents.remediate({"suspect_service": "redis", "fault_guess": "pause"}, DEP)
    assert met["source"] == "fallback" and pln["actions"] == [{"action": "unpause_service", "target": "redis"}]
    pln, _ = agents.remediate({"suspect_service": "redis", "fault_guess": "unknown"}, DEP)
    assert [itm["action"] for itm in pln["actions"]] == [*agents.SWEEP, "restart_web"]
    assert agents.remediate({"suspect_service": None, "fault_guess": "unknown"}, DEP)[0]["actions"] == []


def test_attempt2_remediation_gets_history(fake):
    fkl = fake(REM_OK)
    sta = {**blind_state(), "attempts": 1, "diagnosis": {**DIAG_OK, "findings": {}},
           "applied_fixes": [{"action": "start_service", "target": "redis", "attempt": 1, "ok": False, "detail": "x"}]}
    out = graph.remediation_agent(sta, {})
    his = fkl.payload()["previous_attempt"]
    assert set(his) == {"previous_diagnosis", "actions_taken", "new_symptoms"}
    assert his["actions_taken"][0]["ok"] is False and set(his["new_symptoms"]) == BRIEF_KEYS
    assert not keys(his) & (EXP_KEYS - {"target"})  # actions name their target; no experiment fields
    assert out["fix_plan"]["actions"][-1] == {"action": "restart_web", "target": "web"}


# ---- whole graph ----
def test_healthy_run_uses_three_llm_calls(fake):
    fkl = fake(CHAOS_OK, DIAG_OK, REM_OK)
    sta, _ = graph.run("kill redis", "healed")
    rpt = sta["report"]
    assert len(fkl.calls) == 3 and not fkl.errors
    assert rpt["agents"] == {"chaos_agent": ["llm"], "diagnosis_agent": ["llm"], "remediation_agent": ["llm"]}
    assert rpt["diagnosis_correct"] is True and rpt["experiment_detail"]["reason"] == CHAOS_OK["reason"]
    assert rpt["hardening_rounds"] == [{"round": 1, "patches": ["redis_aof redis"], "improved": True}]


def test_retry_rediagnoses_nothing_and_remediates_twice(fake):
    fkl = fake(CHAOS_OK, DIAG_OK, REM_OK, REM_OK)
    sta, _ = graph.run("kill redis", "retry")
    assert len(fkl.calls) == 4 and "previous_attempt" in fkl.payload()
    assert sta["report"]["agents"]["remediation_agent"] == ["llm", "llm"]
    assert sta["report"]["agents"]["diagnosis_agent"] == ["llm"]


def test_report_names_fallback_agents():
    sta, _ = graph.run("kill redis", "healed")
    assert sta["report"]["agents"] == {nam: ["fallback"] for nam in ("chaos_agent", "diagnosis_agent",
                                                                       "remediation_agent")}
    assert sta["report"]["diagnosis_correct"] is True


def test_cli_prints_the_story(fake, capsys):
    fake(CHAOS_OK, DIAG_OK, REM_OK)
    assert cli.main(["kill redis", "--stub", "--scenario", "healed"]) == 0
    out = capsys.readouterr().out
    for txt in ("== Prompt: 'kill redis'", "== Experiment: kill redis", "hypothesis:", "-> CORRECT",
                "== Action attempt 1: start_service(redis)", "== Healed", "== Hardening round 1",
                "== Agents: chaos_agent=llm, diagnosis_agent=llm, remediation_agent=llm"):
        assert txt in out
