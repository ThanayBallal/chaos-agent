import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from setup import patch


class FakeContainer:
    def __init__(self, service, *, name=None, status="running", container_id=None, exec_results=None, update_error=None):
        self.id = container_id or f"id-{service}"
        self.name = name or f"{service}-1"
        self.status = status
        self.labels = {
            patch.PROJECT_LABEL: patch.PROJECT,
            patch.SERVICE_LABEL: service,
        }
        self.exec_results = list(exec_results or [])
        self.update_error = update_error
        self.exec_calls = []
        self.update_calls = []

    def exec_run(self, command):
        self.exec_calls.append(command)
        if self.exec_results:
            result = self.exec_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return SimpleNamespace(exit_code=0, output=b"OK\n")

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        if self.update_error:
            raise self.update_error


class FakeContainers:
    def __init__(self, containers=()):
        self.items = list(containers)
        self.list_calls = []
        self.list_error = None
        self.by_id = {container.id: container for container in self.items}

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.list_error:
            raise self.list_error
        filters = kwargs.get("filters", {}).get("label", [])
        requested = dict(label.split("=", 1) for label in filters)
        return [c for c in self.items if all(c.labels.get(k) == v for k, v in requested.items())]

    def get(self, container_id):
        if container_id not in self.by_id:
            raise KeyError(container_id)
        return self.by_id[container_id]


class FakeClient:
    def __init__(self, *containers):
        self.containers = FakeContainers(containers)


@pytest.fixture

def sandbox(monkeypatch):
    temp = tempfile.TemporaryDirectory(prefix=".patch-tests-", dir=Path(__file__).resolve().parent)
    state_file = Path(temp.name) / "applied_fixes.json"
    monkeypatch.setattr(patch, "STATE_FILE", state_file)
    registered = []
    unregistered = []
    monkeypatch.setattr(patch.guard, "register", lambda fn, info=None: registered.append((fn, info)))
    monkeypatch.setattr(patch.guard, "unregister", lambda fn: unregistered.append(fn))
    patch._REGISTERED.clear()
    yield state_file, registered, unregistered
    patch._REGISTERED.clear()
    temp.cleanup()


def patch_doc(fix="redis_aof", service="redis", patch_id="p1"):
    return {
        "patches": [{
            "id": patch_id,
            "experiment": "e1",
            "service": service,
            "fix": fix,
            "reason": "prevent data loss",
            "expected": "persistence enabled",
        }],
        "code_suggestions": [],
    }


def write_state(state_file, records):
    state_file.write_text(json.dumps({"applied_fixes": records}), encoding="utf-8")


def test_redis_aof_apply_register_and_undo(monkeypatch, sandbox):
    state_file, registered, unregistered = sandbox
    redis = FakeContainer("redis")
    client = FakeClient(redis)
    monkeypatch.setattr(patch, "_docker_client", lambda: client)

    patch.apply(patch_doc())

    assert redis.exec_calls == [["redis-cli", "CONFIG", "SET", "appendonly", "yes"], ["redis-cli", "CONFIG", "SET", "save", "1 1"]]
    assert len(registered) == 1
    assert registered[0][1]["undo_action"] == "redis_aof_disable"
    assert json.loads(state_file.read_text())["applied_fixes"][0]["status"] == "applied"
    registered[0][0]()
    assert redis.exec_calls[2:] == [["redis-cli", "CONFIG", "SET", "appendonly", "no"], ["redis-cli", "CONFIG", "SET", "save", "3600 1 300 100 60 10000"]]
    assert json.loads(state_file.read_text())["applied_fixes"] == []
    assert unregistered == [registered[0][0]]


def test_restart_policy_apply_register_and_undo(monkeypatch, sandbox):
    state_file, registered, _ = sandbox
    checkout = FakeContainer("cart")
    monkeypatch.setattr(patch, "_docker_client", lambda: FakeClient(checkout))
    patch.apply(patch_doc("restart_policy", "cart"))
    assert checkout.update_calls == [{"restart_policy": {"Name": "unless-stopped"}}]
    assert registered[0][1]["undo_action"] == "restart_policy_no"
    registered[0][0]()
    assert checkout.update_calls[-1] == {"restart_policy": {"Name": "no"}}
    assert json.loads(state_file.read_text())["applied_fixes"] == []


def test_discovery_uses_both_compose_labels(monkeypatch, sandbox):
    redis = FakeContainer("redis")
    client = FakeClient(redis)
    monkeypatch.setattr(patch, "_docker_client", lambda: client)
    patch.apply(patch_doc())
    assert client.containers.list_calls[0] == {
        "all": True,
        "filters": {"label": ["com.docker.compose.project=robot-shop", "com.docker.compose.service=redis"]},
    }


@pytest.mark.parametrize("document", [
    patch_doc("mystery", "redis"),
    {"patches": [{"id": "x"}], "code_suggestions": []},
    {"patches": [{**patch_doc()["patches"][0], "extra": 1}], "code_suggestions": []},
    {"patches": [{**patch_doc()["patches"][0], "reason": " "}], "code_suggestions": []},
    {"patches": [{**patch_doc()["patches"][0], "service": "cart"}], "code_suggestions": []},
    {"patches": patch_doc()["patches"], "code_suggestions": [{"file": "x", "line": 0, "suggestion": "y"}]},
])
def test_invalid_documents_rejected_before_docker(monkeypatch, document):
    called = []
    monkeypatch.setattr(patch, "_docker_client", lambda: called.append(True))
    with pytest.raises(patch.PatchError):
        patch.apply(document)
    assert not called


def test_revert_reverse_order_and_state_cleanup(monkeypatch, sandbox):
    state_file, _, _ = sandbox
    redis = FakeContainer("redis")
    cart = FakeContainer("cart")
    client = FakeClient(redis, cart)
    monkeypatch.setattr(patch, "_docker_client", lambda: client)
    patch.apply({"patches": [patch_doc()["patches"][0], patch_doc("restart_policy", "cart", "p2")["patches"][0]], "code_suggestions": []})
    events = []
    original_exec = redis.exec_run
    original_update = cart.update
    redis.exec_run = lambda cmd: (events.append(("redis", cmd)), original_exec(cmd))[1]
    cart.update = lambda **kw: (events.append(("cart", kw)), original_update(**kw))[1]
    patch.revert()
    assert [name for name, _ in events] == ["cart", "redis", "redis"]  # redis_aof undo = appendonly + save
    assert json.loads(state_file.read_text())["applied_fixes"] == []


def test_failed_undo_preserves_state(monkeypatch, sandbox):
    state_file, registered, _ = sandbox
    redis = FakeContainer("redis", exec_results=[(0, b"OK"), (0, b"OK"), (1, b"ERR failed")])
    monkeypatch.setattr(patch, "_docker_client", lambda: FakeClient(redis))
    patch.apply(patch_doc())
    with pytest.raises(patch.PatchError, match="could not be reverted"):
        patch.revert()
    records = json.loads(state_file.read_text())["applied_fixes"]
    assert len(records) == 1 and records[0]["id"] == "p1"


def test_docker_lookup_error_and_missing_container(monkeypatch, sandbox):
    _, _, _ = sandbox
    client = FakeClient()
    monkeypatch.setattr(patch, "_docker_client", lambda: client)
    with pytest.raises(patch.PatchError, match="no container found"):
        patch.apply(patch_doc())
    client.containers.list_error = RuntimeError("daemon unavailable")
    with pytest.raises(patch.PatchError, match="Docker lookup failed"):
        patch.apply(patch_doc())


def test_revert_missing_recorded_container_keeps_state(monkeypatch, sandbox):
    state_file, _, _ = sandbox
    record = {
        "id": "p1", "experiment": "e1", "service": "redis", "fix": "redis_aof",
        "container_id": "gone", "container_name": "redis-1", "undo_action": "redis_aof_disable", "status": "applied",
    }
    write_state(state_file, [record])
    monkeypatch.setattr(patch, "_docker_client", lambda: FakeClient())
    with pytest.raises(patch.PatchError):
        patch.revert()
    assert json.loads(state_file.read_text())["applied_fixes"] == [record]


def test_state_rejects_malformed_json(sandbox):
    state_file, _, _ = sandbox
    state_file.write_text("{", encoding="utf-8")
    with pytest.raises(patch.PatchError, match="cannot read applied-fixes state"):
        patch._read_state()


def test_cli_argument_and_file_validation(monkeypatch, sandbox):
    _, _, _ = sandbox
    with pytest.raises(SystemExit) as missing_command:
        patch.main([])
    assert missing_command.value.code == 2
    with pytest.raises(SystemExit) as missing_file:
        patch.main(["apply"])
    assert missing_file.value.code == 2
    assert patch.main(["apply", "definitely-missing-patch-file.json"]) == 1
    monkeypatch.setattr(patch, "_docker_client", lambda: pytest.fail("invalid JSON reached Docker"))
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=Path(__file__).resolve().parent, delete=False) as f:
        f.write("{")
        invalid_path = f.name
    try:
        assert patch.main(["apply", invalid_path]) == 1
    finally:
        Path(invalid_path).unlink(missing_ok=True)


def test_docker_operation_errors_do_not_claim_application(monkeypatch, sandbox):
    state_file, registered, _ = sandbox
    redis = FakeContainer("redis", exec_results=[RuntimeError("exec failed")])
    monkeypatch.setattr(patch, "_docker_client", lambda: FakeClient(redis))
    with pytest.raises(patch.PatchError, match="exec failed"):
        patch.apply(patch_doc())
    assert json.loads(state_file.read_text())["applied_fixes"] == []
    assert registered == []


def test_restart_update_error_does_not_register(monkeypatch, sandbox):
    state_file, registered, _ = sandbox
    cart = FakeContainer("cart", update_error=RuntimeError("update failed"))
    monkeypatch.setattr(patch, "_docker_client", lambda: FakeClient(cart))
    with pytest.raises(patch.PatchError, match="update failed"):
        patch.apply(patch_doc("restart_policy", "cart"))
    assert json.loads(state_file.read_text())["applied_fixes"] == []
    assert registered == []


def test_redis_aof_partial_apply_is_rolled_back(monkeypatch, sandbox):
    state_file, registered, _ = sandbox
    redis = FakeContainer("redis", exec_results=[(0, b"OK"), (1, b"ERR save failed")])
    monkeypatch.setattr(patch, "_docker_client", lambda: FakeClient(redis))
    with pytest.raises(patch.PatchError, match="save failed"):
        patch.apply(patch_doc())
    assert redis.exec_calls == [["redis-cli", "CONFIG", "SET", "appendonly", "yes"],
                                ["redis-cli", "CONFIG", "SET", "save", "1 1"],
                                ["redis-cli", "CONFIG", "SET", "appendonly", "no"]]
    assert json.loads(state_file.read_text())["applied_fixes"] == []
    assert registered == []


def test_redis_aof_retried_undo_runs_both_settings(monkeypatch, sandbox):
    state_file, registered, _ = sandbox
    redis = FakeContainer("redis", exec_results=[(0, b"OK"), (0, b"OK"), (0, b"OK"), (1, b"ERR save failed")])
    monkeypatch.setattr(patch, "_docker_client", lambda: FakeClient(redis))
    patch.apply(patch_doc())
    with pytest.raises(patch.PatchError, match="could not be reverted"):
        patch.revert()
    assert len(json.loads(state_file.read_text())["applied_fixes"]) == 1
    patch.revert()
    assert redis.exec_calls[-2:] == [["redis-cli", "CONFIG", "SET", "appendonly", "no"], ["redis-cli", "CONFIG", "SET", "save", "3600 1 300 100 60 10000"]]
    assert json.loads(state_file.read_text())["applied_fixes"] == []
