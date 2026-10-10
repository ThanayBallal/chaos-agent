from heal import live, ports
from monitor import runner


def test_start_waits_for_the_probe_to_settle_before_injecting(monkeypatch):
    ev = []
    monkeypatch.setattr(live, "_steady", lambda bas: None)
    monkeypatch.setattr(live.guard, "reset", lambda: None)
    monkeypatch.setattr(runner, "_probe_create", lambda cid: ev.append("probe") or ("sku-1", None))
    monkeypatch.setattr(live.time, "sleep", lambda sec: ev.append(("sleep", sec)))
    monkeypatch.setattr(ports, "inject", lambda exp: ev.append("inject") or {"injected": True, "info": "killed"})
    hel, abr = live._start({"id": "e1", "target": "redis", "fault": "kill", "params": {"duration_s": 30}}, {})
    assert abr is None
    assert ev == ["probe", ("sleep", live.PROBE_SETTLE_S), "inject"]
    assert live.PROBE_SETTLE_S == 3
    assert hel["sku"] == "sku-1" and hel["injected"]


def test_no_probe_and_no_injection_when_not_steady(monkeypatch):
    ev = []
    monkeypatch.setattr(live, "_steady", lambda bas: "not steady: unhealthy redis=exited")
    monkeypatch.setattr(live.guard, "reset", lambda: None)
    monkeypatch.setattr(runner, "_probe_create", lambda cid: ev.append("probe"))
    monkeypatch.setattr(live.time, "sleep", lambda sec: ev.append("sleep"))
    monkeypatch.setattr(ports, "inject", lambda exp: ev.append("inject"))
    hel, abr = live._start({"id": "e1", "target": "redis", "fault": "kill", "params": {"duration_s": 30}}, {})
    assert abr.startswith("not injected") and ev == []
