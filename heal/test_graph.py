"""Heal graph routing tests. No Docker, no LLM, no data/ writes: run `python -m pytest heal -q`."""
import functools
import importlib
import inspect

import pytest

from heal import graph, ports, run as cli
from heal.graph import MAX_ATTEMPTS, MAX_ROUNDS, check_heal, route_approval, route_compare, route_verify

# exact signatures from contracts.md / team decisions, for modules that may not exist yet
SIG = {"review": ["dep"], "heal_fix": ["action", "target"], "make": ["fnd", "pln", "dep", "rsl", "exclude"],
       "apply": ["pat"], "revert_patch": [], "compare": ["rsl_before", "rsl_after", "fnd_before", "fnd_after"],
       "revert_all": []}
HARDEN_TAIL = ["harden_agent", "apply_hardening", "rerun", "compare"]


@pytest.fixture(autouse=True)
def calls(monkeypatch):
    """Wrap every port so tests can see what each node passed and in which order (the stub still runs)."""
    cal = {nam: [] for nam in ports.REAL}
    cal["_order"] = []
    for nam in ports.REAL:
        org = getattr(ports, nam)

        @functools.wraps(org)
        def rec(*arg, _nam=nam, _org=org, **kw):
            cal[_nam].append((arg, kw))
            cal["_order"].append(_nam)
            return _org(*arg, **kw)
        monkeypatch.setattr(ports, nam, rec)
    return cal


def cmp(imp):
    return {"experiments": [{"id": "e1", "improved": imp}]}


# route_approval
def test_rejected_routes_to_report():
    assert route_approval({"experiment": {"rejection": "duration 90s outside 1..60"}}) == "rejected"


def test_approved_routes_on():
    assert route_approval({"experiment": {"rejection": None}}) == "approved"
    assert route_approval({"experiment": {}}) == "approved"


# route_verify (heal deadline)
@pytest.mark.parametrize("att", [1, MAX_ATTEMPTS])
def test_healed_wins_at_any_attempt(att):
    assert route_verify({"verify": {"healed": True}, "attempts": att}) == "healed"


def test_not_healed_first_attempt_retries():
    assert route_verify({"verify": {"healed": False}, "attempts": 1}) == "retry"


def test_not_healed_at_limit_gives_up():
    assert MAX_ATTEMPTS == 2
    assert route_verify({"verify": {"healed": False}, "attempts": MAX_ATTEMPTS}) == "give_up"


def test_missing_attempts_and_verify_retry():
    assert route_verify({}) == "retry"


# route_compare (hardening rounds)
def test_improved_after_round1_finishes():
    assert route_compare({"round": 1, "comparisons": [cmp(True)]}) == "done"


def test_not_improved_after_round1_goes_to_round2():
    assert route_compare({"round": 1, "comparisons": [cmp(False)]}) == "round2"


def test_improved_in_round2_finishes():
    assert route_compare({"round": 2, "comparisons": [cmp(False), cmp(True)]}) == "done"


def test_not_improved_after_round2_is_exhausted():
    assert MAX_ROUNDS == 2
    assert route_compare({"round": 2, "comparisons": [cmp(False), cmp(False)]}) == "done"


# heal catalog
def test_killed_redis_heals_with_start_then_restart_web(calls):
    sta, _ = graph.run("p", "healed")
    assert sta["fix_plan"]["actions"] == [{"action": "start_service", "target": "redis"},
                                          {"action": "restart_web", "target": "web"}]
    assert [arg for arg, _ in calls["heal_fix"]] == [("start_service", "redis"), ("restart_web", "web")]


@pytest.mark.parametrize("fal,act", [("pause", "unpause_service"), ("latency", "remove_latency"),
                                     ("cpu_stress", "stop_cpu_stress")])
def test_non_restart_faults_skip_restart_web(fal, act):
    assert graph.heal_plan(fal, "redis") == [{"action": act, "target": "redis"}]


def test_killed_web_needs_no_extra_restart():
    assert graph.heal_plan("kill", "web") == [{"action": "start_service", "target": "web"}]


@pytest.mark.parametrize("fix", ["redis_aof", "restart_policy", "healthcheck"])
def test_check_heal_rejects_patch_actions(fix):
    with pytest.raises(ValueError, match="not a heal action"):
        check_heal([{"action": fix, "target": "redis"}])


def test_check_heal_requires_restart_web_last():
    with pytest.raises(ValueError, match="last"):
        check_heal([{"action": "start_service", "target": "redis"}])
    with pytest.raises(ValueError, match="last"):
        check_heal([{"action": "restart_web", "target": "web"}, {"action": "start_service", "target": "redis"}])


def test_remediation_patch_action_rejected_before_anything_runs(calls):
    bad = lambda state: {"fix_plan": {"actions": [{"action": "redis_aof", "target": "redis"}]}}  # noqa: E731
    with pytest.raises(ValueError, match="not a heal action"):
        graph.run("p", "healed", nodes={"remediation_agent": bad})
    assert not calls["heal_fix"] and not calls["make"] and not calls["apply"]


# whole graph: heal
def test_rejected_path(calls):
    sta, pth = graph.run("p", "rejected")
    assert pth == ["understand", "chaos_agent", "guard_approve", "report"]
    assert sta["report"]["outcome"] == "rejected"
    assert not calls["make"] and not calls["apply"] and not calls["revert_all"] and not calls["revert_patch"]


def test_retry_path_loops_once():
    sta, pth = graph.run("p", "retry")
    assert pth.count("observe") == 2 and pth.count("apply_fix") == 2
    assert "harden_agent" in pth and sta["report"]["outcome"] == "healed"


def test_unhealed_gives_up_after_two_attempts(calls):
    sta, pth = graph.run("p", "unhealed")
    assert pth.count("apply_fix") == MAX_ATTEMPTS
    assert pth[-3:] == ["verify", "guard_revert", "report"]
    assert "harden_agent" not in pth and not calls["make"] and not calls["apply"]
    assert sta["report"]["outcome"] == "not_healed"
    assert {itm["attempt"] for itm in sta["applied_fixes"]} == {1, 2}


@pytest.mark.parametrize("scn", graph.SCENARIOS)
@pytest.mark.parametrize("hrd", graph.HARDEN)
def test_every_path_ends_in_report(scn, hrd, calls):
    sta, pth = graph.run("p", scn, harden=hrd)
    assert pth[0] == "understand" and pth[-1] == "report"
    assert sta["attempts"] <= MAX_ATTEMPTS and sta.get("round", 0) <= MAX_ROUNDS
    assert ("inject" in pth) == ("guard_revert" in pth) == bool(calls["revert_all"])  # injected => reverted


def test_unknown_knobs_rejected():
    with pytest.raises(ValueError):
        graph.run("p", "explode")
    with pytest.raises(ValueError):
        graph.run("p", harden="round3")


# whole graph: hardening rounds
def test_improved_after_round1_goes_to_report(calls):
    sta, pth = graph.run("p", "healed", harden="round1")
    assert pth[-6:] == HARDEN_TAIL + ["guard_revert", "report"]
    assert pth.count("harden_agent") == 1 and not calls["revert_patch"]
    assert sta["report"]["hardening"] == "improved_round1" and len(sta["comparisons"]) == 1


def test_not_improved_goes_to_round2(calls):
    sta, pth = graph.run("p", "healed", harden="round2")
    assert pth[-11:] == HARDEN_TAIL + ["revert_patch"] + HARDEN_TAIL + ["guard_revert", "report"]
    assert sta["round"] == 2 and sta["report"]["hardening"] == "improved_round2"


def test_not_improved_after_round2_reports_both(calls):
    sta, pth = graph.run("p", "healed", harden="never")
    assert pth.count("harden_agent") == MAX_ROUNDS and pth[-2:] == ["guard_revert", "report"]
    assert sta["report"]["hardening"] == "not_improved"
    assert len(sta["report"]["comparisons"]) == 2 and sta["report"]["comparisons"] == sta["comparisons"]


def test_round_calls_use_exact_arguments(calls):
    sta, _ = graph.run("p", "healed", harden="never")
    (a1, k1), (a2, k2) = calls["make"]
    fnd, pln, dep, rsl = sta["diagnosis"]["findings"], [sta["experiment"]], sta["depmap"], [sta["symptoms"]]
    assert a1 == (fnd, pln, dep) and k1 == {"rsl": rsl}
    (r1, _), _ = calls["compare"]  # round-1 after-results, before round 2's rerun overwrote them
    assert a2 == (r1[3], pln, dep) and k2 == {"rsl": r1[1], "exclude": sta["round1_patches"]}
    assert r1[1] != sta["rsl_after"]
    assert sta["round1_patches"] and all(isinstance(itm, dict) for itm in sta["round1_patches"])
    (p1,), (p2,) = (arg for arg, _ in calls["apply"])
    assert p1["patches"] == sta["round1_patches"] and p2["patches"][0]["fix"] != p1["patches"][0]["fix"]
    for arg, kw in calls["compare"]:  # both rounds compare against the original before-results
        assert arg[0] == rsl and arg[2] == fnd and kw == {}


def test_revert_patch_only_between_rounds(calls):
    graph.run("p", "healed", harden="never")
    ordr = [nam for nam in calls["_order"] if nam in ("make", "apply", "compare", "revert_patch")]
    assert ordr == ["make", "apply", "compare", "revert_patch", "make", "apply", "compare"]
    assert calls["revert_patch"] == [((), {})]


def test_revert_patch_node_refuses_outside_round1():
    with pytest.raises(RuntimeError):
        graph.revert_patch({"round": 2, "hardening": {}}, {})


def test_guard_revert_never_touches_patches(calls):
    sta, _ = graph.run("p", "healed", harden="never")
    assert sta["report"]["reverted_faults"] == ["fault e1 kill on redis"]
    assert calls["revert_all"] == [((), {})] and len(calls["revert_patch"]) == 1  # the one between rounds only


def test_understand_reviews_the_depmap(calls):
    sta, _ = graph.run("p", "rejected")
    assert calls["review"] == [((sta["depmap"],), {})]
    assert sta["experiment"]["target"] == "redis" and sta["experiment"]["fault"] == "kill"  # from the review risk


def test_understand_survives_review_failure(monkeypatch):
    def boom(dep):
        raise FileNotFoundError("no robot-shop source")
    monkeypatch.setattr(ports, "review", boom)
    sta, _ = graph.run("p", "rejected")
    assert sta["app_model"]["source"] == "unavailable"
    assert sta["experiment"]["target"] == "redis"  # default experiment


# exact signatures
@pytest.mark.parametrize("nam", sorted(SIG))
def test_port_signature_matches_target(nam):
    assert list(inspect.signature(getattr(ports, nam)).parameters) == SIG[nam]


def test_make_defaults_rsl_and_exclude_to_none():
    prm = inspect.signature(ports.make).parameters
    assert prm["rsl"].default is None and prm["exclude"].default is None


@pytest.mark.parametrize("nam", ["review", "revert_all"])
def test_port_signature_matches_existing_module(nam):
    mod, fnc = ports.REAL[nam].rsplit(".", 1)
    real = getattr(importlib.import_module(mod), fnc)
    assert list(inspect.signature(real).parameters) == SIG[nam]


# graph plumbing
def test_node_override():
    sta, pth = graph.run("p", "unhealed", nodes={"verify": lambda state: {"verify": {"healed": True}}})
    assert pth.count("verify") == 1 and sta["report"]["outcome"] == "healed"


def test_unknown_node_override_rejected():
    with pytest.raises(ValueError):
        graph.build({"nope": lambda state: {}})


def test_mermaid_has_every_node():
    mmd = graph.build().get_graph().draw_mermaid()
    for nam in graph.NODES:
        assert nam in mmd


# CLI
def test_cli_prints_path(capsys):
    assert cli.main(["kill redis", "--stub", "--scenario", "rejected"]) == 0
    assert "understand -> chaos_agent -> guard_approve -> report" in capsys.readouterr().out


def test_cli_harden_round2(capsys):
    assert cli.main(["kill redis", "--stub", "--scenario", "healed", "--harden", "round2"]) == 0
    assert "hardening=improved_round2" in capsys.readouterr().out


def test_cli_requires_stub():
    assert cli.main(["kill redis"]) == 2
