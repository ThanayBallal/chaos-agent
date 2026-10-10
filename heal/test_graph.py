"""Heal graph routing tests. No Docker, no LLM, no data/ writes: run `python -m pytest heal -q`."""
import functools
import importlib
import inspect

import pytest

from heal import graph, ports, run as cli
from heal.graph import MAX_ATTEMPTS, route_approval, route_verify

# exact signatures from contracts.md / team decisions, for modules that may not exist yet
SIG = {"review": ["dep"], "make": ["fnd", "pln", "dep", "rsl"], "apply": ["pat"],
       "compare": ["rsl_before", "rsl_after", "fnd_before", "fnd_after"], "revert_all": []}


@pytest.fixture(autouse=True)
def calls(monkeypatch):
    """Wrap every port so tests can see what each node passed (the stub still runs)."""
    cal = {nam: [] for nam in ports.REAL}
    for nam in ports.REAL:
        org = getattr(ports, nam)

        @functools.wraps(org)
        def rec(*arg, _nam=nam, _org=org, **kw):
            cal[_nam].append((arg, kw))
            return _org(*arg, **kw)
        monkeypatch.setattr(ports, nam, rec)
    return cal


# route_approval
def test_rejected_routes_to_report():
    assert route_approval({"experiment": {"rejection": "duration 90s outside 1..60"}}) == "rejected"


def test_approved_routes_on():
    assert route_approval({"experiment": {"rejection": None}}) == "approved"
    assert route_approval({"experiment": {}}) == "approved"


# route_verify
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


# whole graph
def test_rejected_path(calls):
    sta, pth = graph.run("p", "rejected")
    assert pth == ["understand", "chaos_agent", "guard_approve", "report"]
    assert sta["report"]["outcome"] == "rejected"
    assert not calls["make"] and not calls["apply"] and not calls["revert_all"]


def test_healed_path_hardens_then_reverts(calls):
    sta, pth = graph.run("p", "healed")
    assert pth.count("observe") == 1
    assert pth[-7:] == ["verify", "harden_agent", "apply_hardening", "rerun", "compare", "guard_revert", "report"]
    assert sta["report"]["outcome"] == "healed" and sta["attempts"] == 1


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
    assert len(sta["applied_fixes"]) == MAX_ATTEMPTS


@pytest.mark.parametrize("scn", graph.SCENARIOS)
def test_every_path_ends_in_report(scn, calls):
    sta, pth = graph.run("p", scn)
    assert pth[0] == "understand" and pth[-1] == "report"
    assert sta["attempts"] <= MAX_ATTEMPTS
    assert ("inject" in pth) == ("guard_revert" in pth) == bool(calls["revert_all"])  # injected => reverted


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        graph.run("p", "explode")


# port calls
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


def test_harden_passes_before_results(calls):
    sta, _ = graph.run("p", "healed")
    ((arg, kw),) = calls["make"]
    assert arg == (sta["diagnosis"]["findings"], [sta["experiment"]], sta["depmap"])
    assert kw == {"rsl": [sta["symptoms"]]}


def test_apply_hardening_applies_the_patch(calls):
    sta, _ = graph.run("p", "healed")
    assert calls["apply"] == [((sta["hardening"]["patch"],), {})]
    assert sta["hardening"]["applied"] == [pch["id"] for pch in sta["hardening"]["patch"]["patches"]]


def test_compare_gets_before_and_after(calls):
    sta, _ = graph.run("p", "healed")
    ((arg, kw),) = calls["compare"]
    assert arg == ([sta["symptoms"]], sta["rerun"]["results"], sta["diagnosis"]["findings"], sta["rerun"]["findings"])
    assert sta["rerun"]["comparison"]["experiments"][0]["error_rate"] == [sta["symptoms"]["error_rate"], 0.05]


def test_guard_revert_never_lists_patches():
    sta, _ = graph.run("p", "retry")
    rev = sta["report"]["reverted_faults"]
    assert rev == ["fault e1 kill on redis"]
    assert not any(pch["id"] in txt for txt in rev for pch in sta["hardening"]["patch"]["patches"])


# exact signatures
@pytest.mark.parametrize("nam", sorted(SIG))
def test_port_signature_matches_target(nam):
    assert list(inspect.signature(getattr(ports, nam)).parameters) == SIG[nam]


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


def test_cli_requires_stub():
    assert cli.main(["kill redis"]) == 2
