"""Heal catalog tests. No Docker: containers are fakes. Run `python -m pytest heal -q`."""
import inspect
from types import SimpleNamespace

import docker
import pytest

from heal import fixes, graph
from monitor import injector
from safety.allowlist import LBL, PRJ, SVC_LBL


class Con:
    def __init__(self, svc, status="running", prj=PRJ):
        self.labels, self.status, self.id = {LBL: prj, SVC_LBL: svc}, status, f"id-{svc}"
        self.name, self.calls = f"{prj}-{svc}-1", []
        self.res = SimpleNamespace(exit_code=0, output=b"killed 42")

    def reload(self):
        pass

    def start(self):
        self.calls.append("start")
        self.status = "running"

    def unpause(self):
        self.calls.append("unpause")
        self.status = "running"

    def restart(self, timeout=None):
        self.calls.append("restart")
        self.status = "running"

    def exec_run(self, cmd, environment=None):
        self.calls.append(("exec", cmd, environment))
        return self.res


@pytest.fixture
def box(monkeypatch):
    """Fake docker: containers.list returns every container in box["cons"] (as if the filter were ignored)."""
    box = {"cons": [], "filters": []}

    def lst(all=False, filters=None):
        box["filters"].append(filters)
        return list(box["cons"])
    cli = SimpleNamespace(containers=SimpleNamespace(list=lst))
    monkeypatch.setattr(injector, "_docker", lambda: cli)
    return box


def cerr(code, msg):
    return docker.errors.ContainerError("c", code, "tc", injector.NET, msg)


def test_catalog_matches_graph():
    assert set(fixes.ACTIONS) == set(graph.HEAL_CATALOG)


def test_lookup_uses_project_and_service_labels(box):
    box["cons"] = [Con("redis")]
    assert fixes.heal("start_service", "redis")["ok"]
    assert box["filters"] == [{"label": [f"{LBL}={PRJ}", f"{SVC_LBL}=redis"]}]


@pytest.mark.parametrize("cons,why", [([], "found 0"), ([Con("redis", prj="other")], "found 0"),
                                      ([Con("cart")], "found 0"), ([Con("redis"), Con("redis")], "found 2")])
def test_rejects_targets_outside_robot_shop(box, cons, why):
    box["cons"] = cons
    out = fixes.heal("start_service", "redis")
    assert not out["ok"] and out["detail"].startswith("rejected") and why in out["detail"]
    assert not any(con.calls for con in cons)


def test_rejects_unknown_action_without_docker(box):
    out = fixes.heal("redis_aof", "redis")
    assert out == {"action": "redis_aof", "target": "redis", "ok": False, "detail": out["detail"]}
    assert "not in heal catalog" in out["detail"] and not box["filters"]


def test_restart_web_only_on_web(box):
    box["cons"] = [Con("redis"), Con("web")]
    assert not fixes.heal("restart_web", "redis")["ok"]
    box["cons"] = [Con("web")]
    assert fixes.heal("restart_web", "web")["ok"] and box["cons"][0].calls == ["restart"]


def test_start_service(box):
    con = Con("redis", "exited")
    box["cons"] = [con]
    assert fixes.heal("start_service", "redis") == {"action": "start_service", "target": "redis", "ok": True,
                                                    "detail": "started (was exited)"}
    assert fixes.heal("start_service", "redis")["detail"] == "already running" and con.calls == ["start"]


def test_start_service_refuses_paused_and_reports_stuck(box, monkeypatch):
    box["cons"] = [Con("redis", "paused")]
    assert not fixes.heal("start_service", "redis")["ok"]
    con = Con("redis", "exited")
    con.start = lambda: con.calls.append("start")  # never comes up
    box["cons"] = [con]
    monkeypatch.setattr(fixes, "WAIT_S", 0)
    assert not fixes.heal("start_service", "redis")["ok"]


def test_unpause_service(box):
    con = Con("redis", "paused")
    box["cons"] = [con]
    assert fixes.heal("unpause_service", "redis")["ok"] and con.calls == ["unpause"]
    assert fixes.heal("unpause_service", "redis")["ok"] and con.calls == ["unpause"]  # already running: no-op


def test_heal_never_raises(box):
    con = Con("redis", "paused")
    con.unpause = lambda: (_ for _ in ()).throw(docker.errors.APIError("boom"))
    box["cons"] = [con]
    out = fixes.heal("unpause_service", "redis")
    assert not out["ok"] and "APIError" in out["detail"]


@pytest.mark.parametrize("msg", [b"Error: Cannot delete qdisc with handle of zero.\n",
                                 b"RTNETLINK answers: No such file or directory\n"])
def test_remove_latency_ignores_no_such_qdisc(box, monkeypatch, msg):
    box["cons"] = [Con("redis")]
    cmd = []

    def tc(con, txt):
        cmd.append(txt)
        raise cerr(2, msg) if "del" in txt else cerr(1, b"")  # grep -q finds no netem: exit 1
    monkeypatch.setattr(injector, "_tc", tc)
    out = fixes.heal("remove_latency", "redis")
    assert out["ok"] and "no qdisc" in out["detail"]
    assert cmd[0] == f"tc qdisc del dev {injector.DEV} root"


def test_remove_latency_fails_on_other_errors_or_leftover_netem(box, monkeypatch):
    box["cons"] = [Con("redis")]
    monkeypatch.setattr(injector, "_tc", lambda con, txt: (_ for _ in ()).throw(cerr(2, b"Operation not permitted")))
    assert not fixes.heal("remove_latency", "redis")["ok"]
    monkeypatch.setattr(injector, "_tc", lambda con, txt: None)  # del ok, but grep still finds netem
    out = fixes.heal("remove_latency", "redis")
    assert not out["ok"] and "still" in out["detail"]


def test_remove_latency_on_stopped_container_is_clean(box, monkeypatch):
    box["cons"] = [Con("redis", "exited")]
    monkeypatch.setattr(injector, "_tc", lambda con, txt: pytest.fail("no tc on a stopped container"))
    assert fixes.heal("remove_latency", "redis")["ok"]


def test_cpu_signature_matches_the_injector():
    src = inspect.getsource(injector._cpu)
    assert fixes.CPU_SIG in src and "{PID}" in src


def test_stop_cpu_stress_passes_signature_by_env_only(box):
    con = Con("redis")
    box["cons"] = [con]
    out = fixes.heal("stop_cpu_stress", "redis")
    (_, cmd, env), = con.calls
    assert out["ok"] and env == {"SIG": fixes.CPU_SIG, "PIDF": injector.PID}
    assert fixes.CPU_SIG not in " ".join(cmd) and injector.PID not in " ".join(cmd)  # never matches itself


def test_stop_cpu_stress_reports_survivors_and_paused(box):
    con = Con("redis")
    con.res = SimpleNamespace(exit_code=1, output=b"alive 42")
    box["cons"] = [con]
    assert fixes.heal("stop_cpu_stress", "redis") == {"action": "stop_cpu_stress", "target": "redis", "ok": False,
                                                      "detail": "alive 42"}
    box["cons"] = [Con("redis", "paused")]
    assert not fixes.heal("stop_cpu_stress", "redis")["ok"]
