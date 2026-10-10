"""Heal graph routing tests. No Docker, no LLM, no data/ writes: run `python -m pytest heal -q`."""
import functools
import importlib
import inspect
import json

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
    """Wrap every port so tests can see what each node passed and in which order (the stub still runs).
    CHAOS_NO_LLM: the agents use their rule-based fallbacks, never a real LLM (even with a key in .env)."""
    monkeypatch.setenv("CHAOS_NO_LLM", "1")
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


def test_cli_rejects_stub_and_live():
    assert cli.main(["kill redis", "--stub", "--live"]) == 2


# abort routing (live guard: deadline, kill switch, collateral, failed inject)
def test_route_abort():
    assert graph.route_abort({"abort": "heal deadline reached"}) == "abort"
    assert graph.route_abort({"abort": None}) == graph.route_abort({}) == "ok"


def test_verify_abort_gives_up_unless_healed():
    assert route_verify({"verify": {"healed": False}, "attempts": 1, "abort": "kill switch"}) == "give_up"
    assert route_verify({"verify": {"healed": True}, "attempts": 1, "abort": "late"}) == "healed"


@pytest.mark.parametrize("nod,tail", [("inject", ["inject", "guard_revert", "report"]),
                                      ("observe", ["inject", "observe", "guard_revert", "report"])])
def test_abort_goes_to_guard_revert(nod, tail, calls):
    sta, pth = graph.run("p", "healed", nodes={nod: lambda state: {"abort": "kill switch (STOP file)"}})
    assert pth[-len(tail):] == tail and "apply_fix" not in pth and not calls["heal_fix"]
    assert sta["report"]["outcome"] == "aborted" and sta["report"]["abort"] == "kill switch (STOP file)"
    assert calls["revert_all"] == [((), {})]


def test_verify_abort_skips_the_retry(calls):
    stop = lambda state: {"verify": {"healed": False}, "abort": "heal deadline reached"}  # noqa: E731
    sta, pth = graph.run("p", "retry", nodes={"verify": stop})
    assert pth.count("apply_fix") == 1 and pth[-3:] == ["verify", "guard_revert", "report"]
    assert sta["report"]["outcome"] == "aborted"


def test_applied_fixes_keep_heal_results():
    sta, _ = graph.run("p", "healed")
    assert sta["applied_fixes"][0] == {"action": "start_service", "target": "redis", "attempt": 1, "ok": True,
                                       "detail": "stub"}
    assert sta["report"]["stub"] is True and sta["report"]["applied_fixes"] == sta["applied_fixes"]


# ports: live mode adaptations (real modules monkeypatched, no Docker)
@pytest.fixture
def live(monkeypatch, tmp_path):
    from safety import guard
    monkeypatch.setattr(guard, "ACT", tmp_path / "active_faults.json")  # guard.register must not write data/
    monkeypatch.setattr(guard, "_rev", [])
    ports.use_live(True)
    yield
    ports.use_live(False)
    ports._faults.clear()


@pytest.mark.parametrize("nam,arg", [("approve", ({}, {})), ("inject", ({},)), ("observe", ({},)),
                                     ("verify", ({}, 0, 0))])
def test_live_only_ports_refuse_stub_mode(nam, arg):
    with pytest.raises(RuntimeError, match="live-only"):
        getattr(ports, nam)(*arg)


def test_live_make_maps_exclude_and_never_saves(live, monkeypatch):
    from agent import patcher
    got = []
    monkeypatch.setattr(patcher, "make", lambda *arg, **kw: got.append((arg, kw)) or {"patches": []})
    ports.make({"findings": []}, [], {}, rsl=[{"id": "e1"}])
    ports.make({"findings": []}, [], {}, exclude=[{"fix": "redis_aof", "service": "redis"}])
    assert got[0][1] == {"rsl": [{"id": "e1"}], "exclude": (), "save": False}
    assert got[1][1]["exclude"] == [{"fix": "redis_aof", "service": "redis"}] and got[1][1]["save"] is False


PATCH = {"id": "p1", "experiment": "e1", "service": "redis", "fix": "redis_aof", "reason": "r", "expected": "e"}


def test_live_apply_strips_keys_skips_empty_and_feeds_compare(live, monkeypatch):
    from setup import patch
    got = []
    monkeypatch.setattr(patch, "apply", got.append)
    ports.apply({"patches": [], "code_suggestions": [], "stub": True})
    assert got == []  # setup.patch.apply refuses an empty patch list
    ports.apply({"patches": [PATCH], "code_suggestions": [], "stub": True})
    assert got == [{"patches": [PATCH], "code_suggestions": []}]
    res = {"id": "e1", "target": "redis", "fault": "kill", "error_rate": 0.6, "data_survived": False}
    cmp = ports.compare([res], [{**res, "data_survived": True}], {"resilience_score": 50}, {"resilience_score": 80})
    assert cmp["patch_ids"] == ["p1"] and cmp["experiments"][0]["improved"] is True and "stub" not in cmp


def test_live_revert_all_reverts_faults_not_patches(live, monkeypatch):
    from monitor import injector
    from safety import guard
    ran = []
    flt, pat = lambda: ran.append("fault"), lambda: ran.append("patch")  # noqa: E731
    flt.info = "fake"
    monkeypatch.setattr(injector, "inject", lambda exp: flt)
    guard.register(pat, info={"fix": "redis_aof"})  # what setup.patch.apply does
    assert ports.inject({"id": "e1", "target": "redis", "fault": "kill"}) == {"injected": True, "info": "fake"}
    assert [itm[0] for itm in guard._rev] == [pat, flt]
    ports.revert_all()
    assert ran == ["fault"] and [itm[0] for itm in guard._rev] == [pat]


def test_live_inject_failure_reverts_and_reports(live, monkeypatch):
    from monitor import injector
    from safety import guard
    ran = []
    monkeypatch.setattr(injector, "inject", lambda exp: lambda: ran.append("revert"))
    monkeypatch.setattr(guard, "register", lambda fnc, info=None: (_ for _ in ()).throw(OSError("disk full")))
    out = ports.inject({"id": "e1", "target": "redis", "fault": "kill"})
    assert out == {"injected": False, "error": "OSError: disk full"} and ran == ["revert"] and not ports._faults


def test_heal_check_ignores_error_rate_but_not_deadline_or_collateral(monkeypatch):
    import time
    from safety import guard
    monkeypatch.setattr(guard, "kill_switch", lambda: False)
    dep = graph._load("mock_depmap.json")
    assert guard.approve({"target": "redis", "fault": "kill", "params": {"duration_s": 10}}, dep, mock=True) is None
    bas = {"health": {svc["name"]: "healthy" for svc in dep["services"]}}
    smp = {"error_rate": 1.0, "p95_ms": 60000, "health": {**bas["health"], "redis": "unhealthy"}}
    far = time.monotonic() + 60
    assert ports.heal_check(smp, bas, "redis", far) is None  # inside the blast radius, error/p95 aborts off
    assert ports.heal_check(smp, bas, "redis", time.monotonic() - 1) == "heal deadline reached"
    out = [svc for svc in bas["health"] if svc not in guard.blast_radius(dep, "redis")][0]
    assert "collateral" in ports.heal_check({"health": {**smp["health"], out: "unhealthy"}}, bas, "redis", far)
    monkeypatch.setattr(guard, "kill_switch", lambda: True)
    assert "kill switch" in ports.heal_check(smp, bas, "redis", far)


# real hardening modules accept exactly what the ports pass them
def test_real_hardening_signatures_bind_port_arguments():
    from agent import patcher
    from monitor import compare as cmp_mod
    from setup import patch
    inspect.signature(patcher.make).bind({}, [], {}, rsl=[], exclude=(), save=False)
    inspect.signature(patch.apply).bind({})
    inspect.signature(patch.revert).bind()
    inspect.signature(cmp_mod.compare).bind([], [], {}, {}, pat=None)


# hardening honesty
def pair(err=(0.3, 0.3), rec=(20, 20), srv=(True, True), scr=(80, 80)):
    return {"score": {"before": scr[0], "after": scr[1]},
            "experiments": [{"id": "e1", "error_rate": list(err), "recovery_s": list(rec), "data_survived": list(srv)}]}


PAT = [{"id": "p1", "fix": "restart_policy", "service": "cart"}]


@pytest.mark.parametrize("cmp,pts,imp,why", [
    (pair(srv=(False, True)), [], False, "no patch applied"),
    (pair(srv=(False, True)), PAT, True, "data_survived false -> true"),
    (pair(err=(0.0, 0.219), scr=(98, 85)), PAT, False, "score 98 -> 85"),  # live redis run: regressed
    (pair(err=(0.0, 0.219), srv=(False, True)), PAT, False, "error_rate 0.0 -> 0.219"),  # gain but regressed
    (pair(err=(0.10, 0.15), srv=(False, True)), PAT, True, "data_survived"),  # +0.05 is within noise
    (pair(err=(0.30, 0.20)), PAT, True, "error_rate 0.3 -> 0.2"),
    (pair(rec=(30, 18)), PAT, True, "recovery 30s -> 18s"),  # -40% and -12 s
    (pair(rec=(20, 12)), PAT, False, "no gain beyond noise"),  # -40% but only 8 s
    (pair(rec=(100, 85)), PAT, False, "no gain beyond noise"),  # -15 s but only 15%
    (pair(), PAT, False, "no gain beyond noise")])
def test_verdict_is_honest(cmp, pts, imp, why):
    out = graph.verdict(cmp, pts)
    assert out["improved"] is imp and why in out["why"]


def test_no_patch_skips_rerun_and_is_not_applicable(monkeypatch, calls, capsys):
    monkeypatch.setattr(ports, "make", lambda *arg, **kw: {"patches": [], "code_suggestions": []})
    sta, pth = graph.run("p", "healed")
    assert pth[-3:] == ["harden_agent", "guard_revert", "report"] and "rerun" not in pth
    assert not calls["apply"] and not calls["compare"]
    rpt = sta["report"]
    assert rpt["hardening"] == "not_applicable" and rpt["hardening_note"] == graph.NO_FIX
    assert rpt["outcome"] == "healed" and rpt["hardening_rounds"] == [] and rpt["data_survived"]["rerun"] == []


def test_round2_without_a_patch_is_not_improved(monkeypatch):
    org = ports.make
    monkeypatch.setattr(ports, "make", lambda fnd, pln, dep, rsl=None, exclude=None:
                        org(fnd, pln, dep, rsl=rsl) if exclude is None else {"patches": [], "code_suggestions": []})
    sta, pth = graph.run("p", "healed", harden="never")
    assert pth[-4:] == ["revert_patch", "harden_agent", "guard_revert", "report"]
    assert sta["report"]["hardening"] == "not_improved" and graph.NO_FIX in sta["report"]["hardening_note"]


def test_compare_carries_the_verdict():
    sta, _ = graph.run("p", "healed", harden="never")
    assert all(itm["verdict"] == {"improved": False, "why": "no gain beyond noise"} for itm in sta["comparisons"])
    assert [itm["improved"] for itm in sta["report"]["hardening_rounds"]] == [False, False]


def test_report_shows_data_survived_before_and_rerun(capsys):
    assert cli.main(["kill redis", "--stub", "--scenario", "healed"]) == 0
    out = capsys.readouterr().out
    assert "== Data survived (cart probe): before=False (cart lost), rerun 1=True (cart kept)" in out
    assert "before -> after: error_rate=[0.62, 0.05]" in out and "improved=True (data_survived false -> true" in out


def test_cli_prints_not_applicable(monkeypatch, capsys):
    monkeypatch.setattr(ports, "make", lambda *arg, **kw: {"patches": [], "code_suggestions": []})
    assert cli.main(["kill redis", "--stub", "--scenario", "healed"]) == 0
    assert f"== Hardening: not applicable ({graph.NO_FIX})" in capsys.readouterr().out


# heal_report.json (live only)
def test_stub_run_never_writes_the_report(monkeypatch, tmp_path):
    monkeypatch.setattr(graph, "REPORT", tmp_path / "heal_report.json")
    graph.run("p", "healed")
    assert not (tmp_path / "heal_report.json").exists()


def test_save_report_has_everything(tmp_path):
    sta, pth = graph.run("kill redis", "healed")
    out = graph.save_report(sta["report"], pth, tmp_path / "heal_report.json")
    rpt = json.loads(out.read_text(encoding="utf-8"))
    assert rpt["path"] == pth and rpt["prompt"] == "kill redis"
    assert set(rpt["experiment_detail"]) >= {"target", "fault", "params", "reason", "hypothesis"}
    assert set(rpt["symptoms"]) >= {"error_rate", "unhealthy", "containers_not_running"}
    assert set(rpt["diagnosis"]) >= {"suspect_service", "fault_guess", "evidence", "confidence"}
    assert rpt["diagnosis_correct"] is True and rpt["healed"] is True and "healed_in_s" in rpt
    assert rpt["applied_fixes"][0]["ok"] is True and rpt["agents"]["chaos_agent"] == ["fallback"]
    (rnd,) = rpt["hardening_rounds"]
    assert {"patches", "error_rate", "recovery_s", "data_survived", "score", "improved", "why"} <= set(rnd)


# kill switch during hardening
def test_stop_before_round1_applies_nothing(monkeypatch, calls, capsys):
    from safety import guard
    monkeypatch.setattr(guard, "kill_switch", lambda: True)
    sta, pth = graph.run("p", "healed")
    assert pth[-3:] == ["harden_agent", "guard_revert", "report"] and not calls["apply"]
    assert sta["report"]["outcome"] == "stopped_by_kill_switch" and sta["report"]["healed"] is True
    assert calls["revert_all"] == [((), {})]


def test_stop_after_round1_skips_round2(monkeypatch, calls):
    from safety import guard
    flg = {"stop": False}
    org = ports.apply
    monkeypatch.setattr(guard, "kill_switch", lambda: flg["stop"])
    monkeypatch.setattr(ports, "apply", lambda pat: (org(pat), flg.update(stop=True)))  # STOP during the rerun
    sta, pth = graph.run("p", "healed", harden="never")
    assert pth[-3:] == ["compare", "guard_revert", "report"]
    assert pth.count("harden_agent") == 1 and not calls["revert_patch"] and len(calls["apply"]) == 1
    assert sta["report"]["outcome"] == "stopped_by_kill_switch" and sta["report"]["stopped"] == graph.KILL


def test_cli_prints_stop(monkeypatch, capsys):
    from safety import guard
    monkeypatch.setattr(guard, "kill_switch", lambda: True)
    assert cli.main(["kill redis", "--stub", "--scenario", "healed"]) == 0
    out = capsys.readouterr().out
    assert "outcome=stopped_by_kill_switch" in out and "== Healed" in out and "== Stopped by kill switch" in out


def test_apply_hardening_checks_stop_itself(monkeypatch, calls):
    from safety import guard
    monkeypatch.setattr(guard, "kill_switch", lambda: True)
    out = graph.apply_hardening({"round": 2, "hardening": {"round": 2, "patch": {"patches": PAT}}}, {})
    assert out["stopped"] == graph.KILL and not calls["apply"] and graph.route_stop(out) == "stop"


def test_stop_routes(monkeypatch):
    from safety import guard
    monkeypatch.setattr(guard, "kill_switch", lambda: False)
    sta = {"round": 1, "comparisons": [cmp(False)], "hardening": {"patch": {"patches": PAT}}}
    assert route_compare(sta) == "round2" and graph.route_harden(sta) == "apply"
    assert route_compare({**sta, "stopped": graph.KILL}) == "done" and graph.route_harden({**sta, "stopped": "x"}) == "skip"
    monkeypatch.setattr(guard, "kill_switch", lambda: True)
    assert route_compare(sta) == "done" and graph.route_harden(sta) == "skip"


# hold (--pause-before-heal): inject -> hold -> observe, kill switch armed during the hold
@pytest.fixture
def holding(monkeypatch):
    """Short hold timings and a fake Enter: hold.set() presses it."""
    import threading
    monkeypatch.setattr(graph, "HOLD_S", 0.3)
    monkeypatch.setattr(graph, "STATUS_S", 0.1)
    monkeypatch.setattr(graph, "TICK_S", 0.01)
    evt = threading.Event()
    monkeypatch.setattr(graph, "_enter", lambda: evt)
    return evt


def after(pth, nam):
    return pth[pth.index(nam) + 1]


def test_hold_enter_continues_to_observe(holding, capsys):
    holding.set()
    sta, pth = graph.run("kill redis", "healed", hold=True)
    assert pth[pth.index("inject"):pth.index("inject") + 4] == ["inject", "hold", "observe", "diagnosis_agent"]
    assert sta["report"]["outcome"] == "healed" and sta["report"]["held_by"] == "enter"
    assert "BROKEN: kill redis" in capsys.readouterr().out


def test_stop_during_hold_goes_to_guard_revert(holding, monkeypatch, calls):
    from safety import guard
    monkeypatch.setattr(guard, "kill_switch", lambda: True)
    sta, pth = graph.run("kill redis", "healed", hold=True)
    assert pth[pth.index("inject"):] == ["inject", "hold", "guard_revert", "report"]
    assert "observe" not in pth and calls["revert_all"]
    assert sta["report"]["outcome"] == "aborted" and sta["report"]["abort"] == graph.KILL


def test_hold_timeout_continues(holding, capsys):
    sta, pth = graph.run("kill redis", "healed", hold=True)
    assert after(pth, "hold") == "observe"
    assert sta["report"]["held_by"] == "timeout" and sta["report"]["outcome"] == "healed"
    assert "[heal] hold: 0s/0.3s" in capsys.readouterr().out  # status line with container states


def test_ctrl_c_during_hold_goes_to_guard_revert(holding, monkeypatch):
    import signal
    import threading
    monkeypatch.setattr(graph, "_enter", lambda: threading.Timer(0.05, signal.raise_signal, (signal.SIGINT,)).start()
                        or holding)
    org = signal.getsignal(signal.SIGINT)
    sta, pth = graph.run("kill redis", "healed", hold=True)
    assert after(pth, "hold") == "guard_revert" and sta["report"]["abort"] == "Ctrl+C during the hold"
    assert signal.getsignal(signal.SIGINT) is org  # previous (guard) handler restored


def test_hold_does_not_leak_the_experiment_to_diagnosis(holding, monkeypatch):
    from heal import agents
    org, got = agents.diagnose, []
    monkeypatch.setattr(agents, "diagnose", lambda *arg, **kw: got.append((arg, kw)) or org(*arg, **kw))
    graph.run("kill redis", "healed")
    holding.set()
    graph.run("kill redis", "healed", hold=True)
    assert len(got) == 2 and got[0] == got[1]


def test_hold_restarts_the_heal_deadline(holding):
    import time
    holding.set()
    out = graph.hold({"experiment": {"id": "e1", "fault": "kill", "target": "redis", "params": {}},
                      "heal": {"t0": 0.0, "until": 60.0, "injected": True}}, {})
    hel = out["heal"]
    assert out["abort"] is None and hel["injected_t0"] == 0.0 and hel["injected"]
    assert abs(hel["until"] - (time.monotonic() + graph.HEAL_S)) < 1 and hel["until"] - hel["t0"] == graph.HEAL_S


def test_no_hold_by_default():
    assert "hold" not in graph.run("kill redis", "healed")[1]


def test_cli_pause_before_heal_needs_live(capsys):
    assert cli.main(["kill redis", "--stub", "--pause-before-heal"]) == 2


# auto mode (heal.run --auto): the chaos agent picks the top code-review weakness the guard approves
CART = ["const redis = require('redis');", "    redisClient.setex(id, 3600, JSON.stringify(cart));", ""]
CAT = ["        db.collection('products').find({}).toArray()"]


def risk(svc, dep, fil, lin, fal, wgt, dur=30, quote="LLM TEXT, NOT FROM THE FILE"):
    return {"service": svc, "dependency": dep, "file": fil, "line": lin, "quote": quote, "fault": fal,
            "params": {"duration_s": dur, **({"delay_ms": 500} if fal == "latency" else {})}, "weight": wgt,
            "weakness": f"{svc} has no fallback when {dep} fails", "hypothesis": f"{svc} fails while {dep} is down",
            "fix": "add a fallback"}


@pytest.fixture
def reviewed(monkeypatch, tmp_path):
    """A fake robot-shop source tree and a code review citing it: #1 cart/redis, #2 catalogue/mongodb; the top-weight
    risk cites a missing file and one escapes the app, so both are skipped."""
    from agent import code_review
    for rel, lns in (("cart/server.js", CART), ("catalogue/server.js", CAT)):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("\n".join(lns), encoding="utf-8")
    (tmp_path.parent / "outside.js").write_text("secret = 1\n", encoding="utf-8")
    monkeypatch.setattr(code_review, "APP", tmp_path)
    rsk = [risk("user", "mongodb", "user/missing.js", 5, "pause", 9), risk("web", "mysql", "../outside.js", 1, "kill", 8),
           risk("cart", "redis", "cart/server.js", 2, "kill", 3), risk("catalogue", "mongodb", "catalogue/server.js", 1,
                                                                        "pause", 2),
           risk("cart", "redis", "cart/server.js", 3, "latency", 1)]  # blank line: skipped
    monkeypatch.setattr(ports, "review", lambda dep: {"source": "llm", "files_read": 2, "risks": rsk})
    return rsk


def test_auto_with_a_prompt_is_rejected():
    assert cli.main(["kill redis", "--auto", "--stub"]) == 2
    assert cli.main(["--stub"]) == 2  # neither a prompt nor --auto
    with pytest.raises(ValueError):
        graph.run("kill redis", "healed", auto=True)


def test_auto_picks_weakness_1_with_the_required_reason(reviewed, capsys):
    sta, pth = graph.run(None, "healed", auto=True)
    exp, rpt = sta["experiment"], sta["report"]
    assert (exp["target"], exp["fault"]) == ("redis", "kill") and "inject" in pth
    assert exp["reason"].startswith("Chosen autonomously: weakness #1 of 2 from the code review (cart/server.js:2) "
                                    "because ")
    assert rpt["mode"] == "auto" and rpt["auto"] == {"chosen": 1, "skipped": []}
    out = capsys.readouterr().out
    assert out.index("== Code review") < out.index("== Auto: chose weakness #1")
    assert " 1. cart -> redis (kill)  cart/server.js:2" in out and " 2. catalogue -> mongodb (pause)" in out


def test_auto_falls_back_to_weakness_2_when_1_is_rejected(reviewed, capsys):
    reviewed[2]["params"]["duration_s"] = 90  # over the guard's 60 s limit
    sta, pth = graph.run(None, "healed", auto=True)
    exp, aut = sta["experiment"], sta["report"]["auto"]
    assert (exp["target"], exp["fault"]) == ("mongodb", "pause") and "inject" in pth
    assert exp["reason"].startswith("Chosen autonomously: weakness #2 of 2 from the code review "
                                    "(catalogue/server.js:1) because weakness #1 was rejected by the safety guard")
    assert aut["chosen"] == 2 and aut["skipped"][0]["rank"] == 1 and "duration 90s" in aut["skipped"][0]["why"]
    assert "== Auto: skipped weakness #1 (kill redis, cart/server.js:2): rejected by the safety guard: duration 90s" \
        in capsys.readouterr().out


def test_auto_reports_rejected_when_every_weakness_is_rejected(reviewed):
    for itm in reviewed:
        itm["params"]["duration_s"] = 90
    sta, pth = graph.run(None, "healed", auto=True)
    assert pth == ["understand", "chaos_agent", "guard_approve", "report"]
    assert sta["report"]["outcome"] == "rejected" and len(sta["report"]["auto"]["skipped"]) == 2


def test_auto_quotes_code_from_the_file(reviewed):
    from heal import agents
    wks = agents.weaknesses(reviewed, graph._load("mock_depmap.json"))
    assert [(wek["file"], wek["line"]) for wek in wks] == [("cart/server.js", 2), ("catalogue/server.js", 1)]
    assert [wek["code"] for wek in wks] == [CART[1].strip(), CAT[0].strip()]  # the file's line, not the LLM quote
    assert agents.source_line("../outside.js", 1) is None and agents.source_line("cart/server.js", 99) is None


def test_auto_llm_reason_gets_the_prefix(reviewed):
    from heal import agents
    dep = graph._load("mock_depmap.json")
    wek = agents.weaknesses(reviewed, dep)[0]
    raw = {"target": "redis", "fault": "kill", "params": {"duration_s": 20}, "reason": "Because carts live only in redis.",
           "hypothesis": "cart requests fail while redis is down"}
    exp = agents.check_auto(raw, wek, 2, dep)
    assert exp["reason"] == ("Chosen autonomously: weakness #1 of 2 from the code review (cart/server.js:2) because "
                             "carts live only in redis.") and exp["params"] == {"duration_s": 20}
    with pytest.raises(ValueError, match="weakness #1"):
        agents.check_auto({**raw, "target": "mysql"}, wek, 2, dep)


def test_mode_is_recorded_in_heal_report(reviewed, tmp_path):
    out = {}
    for mod, (prm, aut) in {"auto": (None, True), "prompt": ("kill redis", False)}.items():
        sta, pth = graph.run(prm, "healed", auto=aut)
        out[mod] = json.loads(graph.save_report(sta["report"], pth, tmp_path / f"{mod}.json").read_text())
    assert out["auto"]["mode"] == "auto" and [wek["rank"] for wek in out["auto"]["weaknesses"]] == [1, 2]
    assert out["auto"]["weaknesses"][0]["code"] == CART[1].strip()
    assert out["prompt"]["mode"] == "prompt" and out["prompt"]["weaknesses"] is None


def test_refresh_forces_a_new_code_review(monkeypatch):
    monkeypatch.delenv("CHAOS_REVIEW_FRESH", raising=False)
    import os
    assert cli.main(["kill redis", "--stub", "--refresh"]) == 0 and os.environ["CHAOS_REVIEW_FRESH"] == "1"  # undone by monkeypatch
