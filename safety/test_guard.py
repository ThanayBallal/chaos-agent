"""P5: guard tests. No Docker needed: run `python -m pytest safety -q`."""
import json
from pathlib import Path

import pytest

from safety import guard

DEP = json.loads((Path(__file__).resolve().parent.parent / "data" / "mock_depmap.json").read_text())
BAS = {"error_rate": 0.004, "p95_ms": 180}


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "STP", tmp_path / "STOP")
    monkeypatch.setattr(guard, "ACT", tmp_path / "active_faults.json")
    guard.reset()
    guard._rev.clear()
    yield
    guard._rev.clear()
    guard.reset()


def exp(fault="kill", target="redis", **prm):
    return {"id": "e1", "fault": fault, "target": target, "params": prm or {"duration_s": 30}}


def smp(t=0, err=0.0, p95=100, health=None):
    return {"t": t, "error_rate": err, "p95_ms": p95, "health": health or {}}


# allowlist
def test_unknown_fault_rejected():
    assert "allowlist" in guard.approve(exp(fault="nuke"), DEP, mock=True)


def test_target_outside_project_rejected():
    assert "not in project" in guard.approve(exp(target="postgres"), DEP, mock=True)


def test_wrong_params_rejected():
    assert "params" in guard.approve(exp("latency", duration_s=10), DEP, mock=True)


def test_valid_experiment_approved():
    assert guard.approve(exp("latency", duration_s=10, delay_ms=200), DEP, mock=True) is None


# duration cap
@pytest.mark.parametrize("dur,ok", [(60, True), (1, True), (61, False), (0, False), (-5, False)])
def test_duration_cap(dur, ok):
    rej = guard.approve(exp(duration_s=dur), DEP, mock=True)
    assert (rej is None) == ok
    if not ok:
        assert "duration" in rej


# error rate
def test_error_rate_breach_only_after_hold():
    for t in range(10):
        assert guard.check(smp(t=t, err=0.8), BAS, "redis") is None
    assert "error_rate" in guard.check(smp(t=10, err=0.8), BAS, "redis")


def test_error_rate_timer_resets_on_recovery():
    for t in range(9):
        guard.check(smp(t=t, err=0.8), BAS)
    assert guard.check(smp(t=9, err=0.1), BAS) is None
    assert guard.check(smp(t=10, err=0.8), BAS) is None
    assert guard.check(smp(t=19, err=0.8), BAS) is None
    assert guard.check(smp(t=20, err=0.8), BAS) is not None


def test_reset_clears_breach_timer():
    guard.check(smp(t=0, err=0.8), BAS)
    guard.reset()
    assert guard.check(smp(t=10, err=0.8), BAS) is None


# p95
def test_p95_breach_immediate():
    assert "p95" in guard.check(smp(p95=5001), BAS)
    assert guard.check(smp(p95=5000), BAS) is None


# blast radius
def test_blast_radius_transitive():
    assert guard.blast_radius(DEP, "redis") == {"redis", "cart", "user", "shipping", "payment", "web"}
    assert guard.blast_radius(DEP, "dispatch") == {"dispatch"}


def test_collateral_inside_radius_allowed():
    alw = guard.blast_radius(DEP, "redis")
    assert guard.check(smp(health={"redis": "unhealthy", "cart": "unhealthy"}), BAS, "redis", alw) is None


def test_collateral_outside_radius_aborts():
    alw = guard.blast_radius(DEP, "redis")
    rsn = guard.check(smp(health={"cart": "unhealthy", "ratings": "unhealthy"}), BAS, "redis", alw)
    assert "ratings" in rsn and "cart" not in rsn


def test_collateral_fallback_is_target_only():
    assert guard.check(smp(health={"redis": "unhealthy"}), BAS, "redis") is None
    assert "cart" in guard.check(smp(health={"cart": "unhealthy"}), BAS, "redis")


def test_approve_stashes_blast_radius_for_check():
    assert guard.approve(exp(), DEP, mock=True) is None
    assert guard.check(smp(health={"cart": "unhealthy"}), BAS, "redis") is None
    assert "ratings" in guard.check(smp(health={"ratings": "unhealthy"}), BAS, "redis")
    # a different target does not reuse the stash
    assert "cart" in guard.check(smp(health={"cart": "unhealthy"}), BAS, "mysql")


# kill switch
def test_kill_switch():
    assert guard.check(smp(), BAS) is None
    guard.STP.write_text("")
    assert guard.kill_switch()
    assert "kill switch" in guard.check(smp(), BAS)


def test_install_clears_stale_stop(monkeypatch):
    monkeypatch.setattr(guard.signal, "signal", lambda *a: None)
    monkeypatch.setattr(guard.atexit, "register", lambda *a: None)
    guard.STP.write_text("")
    guard.install()
    assert not guard.kill_switch()


# steady state
def test_ready():
    assert guard.ready(smp(err=0.01, health={"cart": "healthy"}), BAS) is None
    assert "unhealthy" in guard.ready(smp(health={"cart": "unhealthy"}), BAS)
    assert "error_rate" in guard.ready(smp(err=0.2), {"error_rate": 0.05})
    assert guard.ready(smp(err=0.1), {"error_rate": 0.05}) is None


# active fault persistence
def test_register_unregister_persist():
    a, b = (lambda: None), (lambda: None)
    guard.register(a, {"target": "redis"})
    guard.register(b)
    assert len(json.loads(guard.ACT.read_text())) == 2
    guard.unregister(a)
    assert [itm["info"] for itm in json.loads(guard.ACT.read_text())] == [None]


def test_revert_all_runs_and_clears():
    ran = []
    guard.register(lambda: ran.append(1))
    guard.register(lambda: 1 / 0)  # failing revert must not stop the others
    guard.revert_all()
    assert ran == [1]
    assert json.loads(guard.ACT.read_text()) == []
