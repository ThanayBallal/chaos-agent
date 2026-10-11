import os
import time

import pytest
from fastapi.testclient import TestClient

from dashboard import server


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "RUN", server.Run())
    monkeypatch.setattr(server, "STP", tmp_path / "STOP")
    return TestClient(server.app)


def wait(cli, sec=120):
    end = time.time() + sec
    while time.time() < end:
        if cli.get("/api/state").json()["status"] != "running":
            return
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_command_building():
    cmd = server.command(server.RunReq(mode="campaign", max=4))
    assert cmd[2:] == ["-m", "heal.run", "--auto", "--all", "--max", "4", "--live", "--pause-first"]
    cmd = server.command(server.RunReq(mode="prompt", prompt="kill redis", pause=False))
    assert cmd[-2:] == ["kill redis", "--live"]
    assert server.command(server.RunReq(mode="auto", stub=True))[-1] == "--stub"
    assert server.command(server.RunReq(mode="campaign", max=99))[7] == "8"
    with pytest.raises(ValueError):
        server.command(server.RunReq(mode="prompt", prompt="  "))
    with pytest.raises(ValueError):
        server.command(server.RunReq(mode="rm -rf"))


def test_index_and_missing_data(cli):
    assert "Chaos Agent" in cli.get("/").text
    assert cli.get("/api/data/nope").status_code == 404


def test_replay_runs_to_the_end_with_heal_now(cli, monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda sec: None)
    assert cli.post("/api/run", json={"mode": "replay"}).json()["ok"]
    assert cli.post("/api/run", json={"mode": "replay"}).status_code == 400  # one run at a time
    cli.post("/api/heal")
    wait(cli)
    evs = [evt for row in server.RUN.since(0) for evt in row["events"]]
    assert sum(1 for evt in evs if evt["t"] == "diagnosis" and evt["correct"]) == 4
    assert [evt["rank"] for evt in evs if evt["t"] == "summary_row"] == [1, 2, 3, 4]


def test_stale_replay_never_writes_into_a_new_run(cli):
    run = server.RUN
    gen = run._reset("replay")
    run._reset("replay")
    assert run._push("stale line", gen) is False and run.evs == []


def test_kill_switch_creates_stop_file(cli):
    server.RUN.mod = "prompt"
    assert cli.post("/api/stop").json()["ok"] and server.STP.exists()


@pytest.mark.skipif(not os.path.isdir(os.path.join(os.path.dirname(server.ROT), "robot-shop")),
                    reason="needs ../robot-shop next to the repo")
def test_real_pipeline_through_the_server_in_stub_mode(cli, monkeypatch):
    monkeypatch.setenv("CHAOS_NO_LLM", "1")
    assert cli.post("/api/run", json={"mode": "prompt", "prompt": "kill redis", "stub": True}).json()["ok"]
    wait(cli, 180)
    evs = [evt for row in server.RUN.since(0) for evt in row["events"]]
    kds = {evt["t"] for evt in evs}
    assert {"chosen", "diagnosis", "healed", "hardening", "agents"} <= kds
    assert server.RUN.cod == 0
