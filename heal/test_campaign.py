"""Campaign tests (heal.run --auto --all), stub mode: no Docker, no LLM, data/ writes only to tmp_path."""
import json

import pytest

from heal import campaign, graph, ports, run as cli
from heal.test_graph import calls, holding, reviewed, risk  # noqa: F401 (fixtures; calls is autouse)
from safety import guard


@pytest.fixture
def three(reviewed):
    """reviewed plus a third readable weakness: #1 kill redis, #2 pause mongodb, #3 latency redis."""
    reviewed.append(risk("cart", "redis", "cart/server.js", 1, "latency", 1.5))
    return reviewed


@pytest.fixture
def files(tmp_path):
    return {"out": tmp_path / "heal_campaign.json", "last": tmp_path / "heal_report.json"}


def cycle_of(rpt, rank):
    return next(cyc for cyc in rpt["cycles"] if cyc["rank"] == rank)


def test_runs_every_weakness_in_rank_order(three, files, calls, capsys):
    rpt = campaign.run(scenario="healed", **files)
    assert rpt["n"] == 3 and rpt["stopped"] is None and rpt["not_run"] == []
    assert [(cyc["rank"], cyc["target"], cyc["fault"], cyc["status"]) for cyc in rpt["cycles"]] == [
        (1, "redis", "kill", "ran"), (2, "mongodb", "pause", "ran"), (3, "redis", "latency", "ran")]
    for cyc in rpt["cycles"]:
        exp = cyc["report"]["experiment_detail"]
        assert (exp["target"], exp["fault"]) == (cyc["target"], cyc["fault"]) and cyc["report"]["outcome"] == "healed"
        assert exp["reason"].startswith(f"Chosen autonomously: weakness #{cyc['rank']} of 3 from the code review "
                                        f"({cyc['where']}) because the campaign tests each ranked weakness")
        assert cyc["report"]["path"][:3] == ["understand", "chaos_agent", "guard_approve"]
    campaign.table(rpt)
    out = capsys.readouterr().out
    assert out.count("== Code review") == 1 and out.count("[heal] understand: code review") == 1  # understand once
    assert out.index("=== Cycle 1/3: redis kill ===") < out.index("=== Cycle 2/3: mongodb pause ===") \
        < out.index("=== Cycle 3/3: redis latency ===") < out.index("=== Campaign summary")
    saved = json.loads(files["out"].read_text())
    assert saved["mode"] == "auto-campaign" and [wek["rank"] for wek in saved["weaknesses"]] == [1, 2, 3]
    assert [cyc["report"]["experiment"] for cyc in saved["cycles"]] == ["e1"] * 3
    assert [itm["rank"] for itm in saved["summary"]] == [1, 2, 3] and saved["summary"][0]["diagnosis_correct"]
    last = json.loads(files["last"].read_text())
    assert last["mode"] == "auto" and last["experiment_detail"]["fault"] == "latency"  # the last cycle's report


def test_max_limits_the_campaign(three, files):
    rpt = campaign.run(scenario="healed", max_n=2, **files)
    assert [cyc["rank"] for cyc in rpt["cycles"]] == [1, 2] and len(rpt["weaknesses"]) == 3
    assert "weakness #2 of 2" in cycle_of(rpt, 2)["report"]["experiment_detail"]["reason"]


def test_hardening_patches_stay_applied_and_are_listed(three, files, calls, capsys):
    rpt = campaign.run(scenario="healed", **files)
    kpt = [(itm["cycle"], itm["id"], itm["fix"], itm["service"]) for itm in rpt["hardening_kept"]]
    assert kpt == [(1, "c1-p1", "redis_aof", "redis"), (2, "c2-p2", "restart_policy", "mongodb"),
                   (3, "c3-p3", "restart_policy", "redis")]  # redis_aof is already kept: not proposed again
    assert not calls["revert_patch"]  # nothing undone between cycles
    campaign.table(rpt)
    assert "c1-p1 redis_aof redis (cycle 1)" in capsys.readouterr().out
    assert ports.kept() == [] and ports._camp["cycle"] is None  # campaign state cleared


def test_revert_patch_keeps_earlier_cycles(three, files, calls, capsys):
    rpt = campaign.run(scenario="healed", harden="round2", max_n=2, **files)  # round 1 never improves
    assert len(calls["revert_patch"]) == 2
    # cycle 1: redis_aof (round 1) undone, restart_policy redis (round 2) kept; cycle 2: mongodb has one fix only
    assert [cyc["report"]["hardening"] for cyc in rpt["cycles"]] == ["improved_round2", "not_improved"]
    assert [itm["id"] for itm in rpt["hardening_kept"]] == ["c1-p2"]
    assert "round-1 patches undone, kept c1-p2" in capsys.readouterr().out  # cycle 2's revert keeps cycle 1's patch


def test_rejected_weakness_is_skipped(three, files, capsys):
    three[2]["params"]["duration_s"] = 90  # weakness #1 (kill redis) is over the guard's 60 s limit
    rpt = campaign.run(scenario="healed", **files)
    one = cycle_of(rpt, 1)
    assert one["status"] == "skipped" and "duration 90s" in one["skip_reason"]
    assert "inject" not in one["report"]["path"] and one["report"]["outcome"] == "rejected"
    assert [cyc["status"] for cyc in rpt["cycles"]] == ["skipped", "ran", "ran"] and rpt["stopped"] is None
    campaign.table(rpt)
    out = capsys.readouterr().out
    assert "== Cycle 1/3: skipped weakness #1: autonomous campaign: weakness #1 rejected by the safety guard" in out
    assert "| skipped: autonomous campaign" in out


def test_not_ready_between_cycles_stops_safely(three, files, calls, monkeypatch):
    monkeypatch.setattr(campaign, "READY_S", 0.01)
    monkeypatch.setattr(ports, "ready", lambda bas: "not steady: unhealthy web=unhealthy")
    rpt = campaign.run(scenario="healed", **files)
    assert [cyc["rank"] for cyc in rpt["cycles"]] == [1] and rpt["not_run"] == [2, 3]
    assert rpt["stopped"].startswith("not steady between cycles: not steady: unhealthy web=unhealthy")
    assert calls["heal_fix"][-1] == (("restart_web", "web"), {})  # tried once before giving up
    assert json.loads(files["out"].read_text())["stopped"] == rpt["stopped"]


def test_restart_web_recovers_between_cycles(three, files, calls, monkeypatch):
    monkeypatch.setattr(campaign, "READY_S", 0.01)
    monkeypatch.setattr(ports, "ready", lambda bas: None if len(calls["heal_fix"]) == 3 else "not steady")
    rpt = campaign.run(scenario="healed", max_n=2, **files)
    assert [cyc["status"] for cyc in rpt["cycles"]] == ["ran", "ran"] and rpt["stopped"] is None


def test_stop_mid_campaign_reverts_and_writes_the_report(three, files, calls, monkeypatch):
    stp, org = {"on": False}, ports.heal_fix
    monkeypatch.setattr(guard, "kill_switch", lambda: stp["on"])
    monkeypatch.setattr(ports, "heal_fix", lambda *arg: stp.update(on=True) or org(*arg))  # STOP while healing #1
    rpt = campaign.run(scenario="healed", **files)
    assert rpt["stopped"] == graph.KILL and [cyc["rank"] for cyc in rpt["cycles"]] == [1] and rpt["not_run"] == [2, 3]
    assert cycle_of(rpt, 1)["report"]["outcome"] == "stopped_by_kill_switch"
    assert calls["revert_all"] and not calls["apply"]  # fault reverted, no hardening applied
    saved = json.loads(files["out"].read_text())
    assert saved["stopped"] == graph.KILL and saved["ended_at"] and files["last"].exists()


def test_ctrl_c_mid_cycle_reverts_and_writes_the_report(three, files, calls, monkeypatch):
    org, num = ports.heal_fix, []

    def fix(*arg):
        num.append(arg)
        if len(num) == 3:  # cycle 2's first heal action (cycle 1: start_service redis, restart_web)
            raise KeyboardInterrupt
        return org(*arg)
    monkeypatch.setattr(ports, "heal_fix", fix)
    rpt = campaign.run(scenario="healed", **files)
    assert rpt["stopped"] == "Ctrl+C" and [cyc["status"] for cyc in rpt["cycles"]] == ["ran", "stopped"]
    assert cycle_of(rpt, 2)["stop_reason"] == "Ctrl+C" and rpt["not_run"] == [3]
    assert calls["revert_all"][-1] == ((), {})  # reverted on the way out
    assert json.loads(files["out"].read_text())["summary"][1]["status"] == "stopped"


@pytest.mark.parametrize("kw, num", [({"first": True}, 1), ({"hold": True}, 2), ({"hold": True, "first": True}, 1),
                                     ({}, 0)])
def test_pause_first_pauses_only_once(three, files, holding, capsys, kw, num):
    holding.set()  # Enter already pressed: every hold continues at once
    rpt = campaign.run(scenario="healed", max_n=2, **files, **kw)
    assert ["hold" in cyc["report"]["path"] for cyc in rpt["cycles"]] == [idx < num for idx in range(2)]
    assert capsys.readouterr().out.count("BROKEN: ") == num


def test_ctrl_c_during_a_hold_stops_the_campaign(three, files, holding, monkeypatch):
    monkeypatch.setattr(graph, "_enter", lambda: holding)
    org = graph.hold
    monkeypatch.setitem(graph.HOLD, "hold", lambda state, config: {**org(state, config), "abort": graph.CTRL_C})
    rpt = campaign.run(scenario="healed", first=True, **files)
    assert rpt["stopped"] == graph.CTRL_C and len(rpt["cycles"]) == 1


def test_cli_campaign_flags(three, monkeypatch, capsys):
    got = []
    org = campaign.run
    monkeypatch.setattr(campaign, "run", lambda **kw: got.append(kw) or org(**kw))
    assert cli.main(["--auto", "--all", "--stub", "--scenario", "healed", "--max", "2"]) == 0
    assert got[0]["max_n"] == 2 and got[0]["out"] is None and got[0]["last"] is None  # stub writes nothing
    out = capsys.readouterr().out
    assert "=== Campaign summary: 2 of 2 cycles ===" in out and "== Mode: auto campaign (weakness #2 of 2" in out
    for bad in (["--auto", "--all"], ["kill redis", "--all", "--stub"], ["--all", "--stub"],
                ["--auto", "--stub", "--max", "3"], ["--auto", "--all", "--stub", "--max", "0"],
                ["--auto", "--all", "--stub", "--pause-first"], ["--auto", "--stub", "--pause-first"]):
        assert cli.main(bad) == 2, bad
    assert len(got) == 1


def test_single_auto_and_prompt_modes_do_not_run_a_campaign(reviewed, monkeypatch):
    monkeypatch.setattr(campaign, "run", lambda **kw: pytest.fail("campaign ran"))
    assert cli.main(["--auto", "--stub", "--scenario", "healed"]) == 0
    assert cli.main(["kill redis", "--stub", "--scenario", "healed"]) == 0
    sta, pth = graph.run(None, "healed", auto=True)
    assert sta["report"]["auto"] == {"chosen": 1, "skipped": []} and "pick" not in sta


def test_pick_needs_auto_and_a_context():
    with pytest.raises(ValueError):
        graph.run("kill redis", pick={"rank": 1, "of": 1})
