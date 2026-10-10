"""Apply and revert the supported live Robot Shop resilience fixes."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from safety import guard

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "applied_fixes.json"
PROJECT = "robot-shop"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
_STATE_LOCK = threading.RLock()
_REGISTERED: dict[str, Callable[[], None]] = {}

PATCH_FIELDS = {"id", "experiment", "service", "fix", "reason", "expected"}
PATCH_ROOT_FIELDS = {"patches", "code_suggestions"}
SUGGESTION_FIELDS = {"file", "line", "suggestion"}
STATE_FIELDS = {
    "id", "experiment", "service", "fix", "container_id", "container_name",
    "undo_action", "status",
}
UNDO_ACTIONS = {"redis_aof": "redis_aof_disable", "restart_policy": "restart_policy_no"}
REDIS_ON = (("appendonly", "yes"), ("save", "1 1"))  # snapshot to /data/dump.rdb within 1 s of a change
REDIS_OFF = (("appendonly", "no"), ("save", "3600 1 300 100 60 10000"))  # redis 6.2 defaults
FIXES = frozenset(UNDO_ACTIONS)


class PatchError(RuntimeError):
    """An invalid patch or an incomplete/failed live operation."""


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_patch(pat: Any) -> list[dict[str, str]]:
    if not isinstance(pat, dict):
        raise PatchError("patch document must be a JSON object")
    if set(pat) != PATCH_ROOT_FIELDS:
        missing = sorted(PATCH_ROOT_FIELDS - set(pat))
        extra = sorted(set(pat) - PATCH_ROOT_FIELDS)
        details = []
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        if extra:
            details.append("unsupported fields: " + ", ".join(extra))
        raise PatchError("patch document has " + "; ".join(details))
    patches = pat["patches"]
    suggestions = pat["code_suggestions"]
    if not isinstance(patches, list) or not patches:
        raise PatchError("patches must be a non-empty array")
    if not isinstance(suggestions, list):
        raise PatchError("code_suggestions must be an array")
    for index, item in enumerate(suggestions):
        if not isinstance(item, dict) or set(item) != SUGGESTION_FIELDS:
            raise PatchError(
                f"code_suggestions[{index}] must contain exactly file, line, suggestion"
            )
        if not all(_nonempty_text(item[key]) for key in ("file", "suggestion")):
            raise PatchError(f"code_suggestions[{index}] file and suggestion must be non-empty strings")
        if isinstance(item["line"], bool) or not isinstance(item["line"], int) or item["line"] < 1:
            raise PatchError(f"code_suggestions[{index}].line must be a positive integer")

    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    for index, item in enumerate(patches):
        if not isinstance(item, dict):
            raise PatchError(f"patches[{index}] must be an object")
        if set(item) != PATCH_FIELDS:
            missing = sorted(PATCH_FIELDS - set(item))
            extra = sorted(set(item) - PATCH_FIELDS)
            details = []
            if missing:
                details.append("missing fields: " + ", ".join(missing))
            if extra:
                details.append("unsupported fields: " + ", ".join(extra))
            raise PatchError(f"patches[{index}] has " + "; ".join(details))
        for field in PATCH_FIELDS:
            if not _nonempty_text(item[field]):
                raise PatchError(f"patches[{index}].{field} must be a non-empty string")
        fix = item["fix"]
        service = item["service"]
        if fix not in FIXES:
            raise PatchError(f"unsupported fix '{fix}'; supported fixes: {', '.join(sorted(FIXES))}")
        if fix == "redis_aof" and service != "redis":
            raise PatchError("redis_aof can only target the redis service")
        if item["id"] in seen_ids:
            raise PatchError(f"duplicate patch id '{item['id']}'")
        target = (fix, service)
        if target in seen_targets:
            raise PatchError(f"duplicate fix '{fix}' for service '{service}'")
        seen_ids.add(item["id"])
        seen_targets.add(target)
        result.append(dict(item))
    return result


def _read_state() -> list[dict[str, str]]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PatchError(f"cannot read applied-fixes state {STATE_FILE}: {err}") from err
    if not isinstance(data, dict) or set(data) != {"applied_fixes"}:
        raise PatchError("applied-fixes state must contain exactly an 'applied_fixes' array")
    records = data["applied_fixes"]
    if not isinstance(records, list):
        raise PatchError("applied_fixes in state must be an array")
    seen_ids: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != STATE_FIELDS:
            raise PatchError(f"applied_fixes[{index}] has an invalid shape")
        for field in STATE_FIELDS - {"status"}:
            if not _nonempty_text(record[field]):
                raise PatchError(f"applied_fixes[{index}].{field} must be a non-empty string")
        if record["fix"] not in FIXES or record["undo_action"] != UNDO_ACTIONS[record["fix"]]:
            raise PatchError(f"applied_fixes[{index}] has an unsupported fix or undo action")
        if record["status"] not in {"applying", "applied"}:
            raise PatchError(f"applied_fixes[{index}].status must be applying or applied")
        target = (record["fix"], record["service"])
        if record["id"] in seen_ids or target in seen_targets:
            raise PatchError("applied-fixes state contains duplicate or inconsistent records")
        seen_ids.add(record["id"])
        seen_targets.add(target)
    return records


def _write_state(records: list[dict[str, str]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"applied_fixes": records}, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".applied_fixes-", suffix=".tmp", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, STATE_FILE)
    except OSError:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _save_record(record: dict[str, str]) -> None:
    with _STATE_LOCK:
        records = _read_state()
        if any(old["id"] == record["id"] for old in records):
            raise PatchError(f"patch id '{record['id']}' is already recorded")
        if any((old["fix"], old["service"]) == (record["fix"], record["service"]) for old in records):
            raise PatchError(f"fix '{record['fix']}' for service '{record['service']}' is already recorded")
        _write_state(records + [dict(record)])


def _update_status(patch_id: str, status: str) -> None:
    with _STATE_LOCK:
        records = _read_state()
        found = False
        for record in records:
            if record["id"] == patch_id:
                record["status"] = status
                found = True
                break
        if not found:
            raise PatchError(f"state record for patch '{patch_id}' disappeared")
        _write_state(records)


def _remove_record(patch_id: str) -> None:
    with _STATE_LOCK:
        records = _read_state()
        remaining = [record for record in records if record["id"] != patch_id]
        if len(remaining) == len(records):
            raise PatchError(f"state record for patch '{patch_id}' disappeared")
        _write_state(remaining)


def _docker_client():
    try:
        import docker
    except ImportError as err:
        raise PatchError("Docker SDK is unavailable; install requirements.txt") from err
    try:
        return docker.from_env()
    except Exception as err:
        raise PatchError(f"could not connect to Docker: {err}") from err


def _find_service(client: Any, service: str):
    filters = {"label": [f"{PROJECT_LABEL}={PROJECT}", f"{SERVICE_LABEL}={service}"]}
    try:
        found = client.containers.list(all=True, filters=filters)
    except Exception as err:
        raise PatchError(f"Docker lookup failed for service '{service}': {err}") from err
    matches = [
        container for container in found
        if getattr(container, "labels", {}).get(PROJECT_LABEL) == PROJECT
        and getattr(container, "labels", {}).get(SERVICE_LABEL) == service
    ]
    if not matches:
        raise PatchError(f"no container found for service '{service}' in Compose project {PROJECT}")
    if len(matches) != 1:
        raise PatchError(f"expected one container for service '{service}', found {len(matches)}")
    return matches[0]


def _find_recorded_container(client: Any, record: dict[str, str]):
    try:
        container = client.containers.get(record["container_id"])
    except Exception as err:
        raise PatchError(
            f"cannot find container {record['container_id']} for service '{record['service']}': {err}"
        ) from err
    labels = getattr(container, "labels", {})
    if labels.get(PROJECT_LABEL) != PROJECT or labels.get(SERVICE_LABEL) != record["service"]:
        raise PatchError(f"recorded container {record['container_id']} no longer has the expected Compose labels")
    return container


def _redis_config(container: Any, key: str, value: str) -> None:
    try:
        result = container.exec_run(["redis-cli", "CONFIG", "SET", key, value])
    except Exception as err:
        raise PatchError(f"Redis CONFIG SET {key} {value} failed: {err}") from err
    if isinstance(result, tuple):
        exit_code, output = result
    else:
        exit_code, output = getattr(result, "exit_code", None), getattr(result, "output", b"")
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    message = str(output).strip()
    if exit_code != 0 or message != "OK":
        raise PatchError(
            f"Redis CONFIG SET {key} {value} returned exit {exit_code}: {message or '<no output>'}"
        )


def _redis_command(container: Any, enabled: bool) -> None:
    """redis_aof on: AOF plus an RDB snapshot within a second of any change; off: back to the defaults.
    A runtime CONFIG SET is lost when redis restarts after a crash, so it reboots with appendonly off and ignores
    the AOF; the RDB snapshot (/data/dump.rdb) is always loaded at startup, which is what makes data survive."""
    if getattr(container, "status", None) != "running":
        raise PatchError(f"Redis container {container.name} is not running")
    settings = REDIS_ON if enabled else REDIS_OFF
    done = 0
    try:
        for key, value in settings:
            _redis_config(container, key, value)
            done += 1
    except PatchError as err:
        if enabled and done:  # do not leave a half-applied fix behind: the caller drops the record
            try:
                for key, value in REDIS_OFF[:done]:
                    _redis_config(container, key, value)
            except PatchError as undo_err:
                raise PatchError(f"{err}; rolling back the partial redis_aof also failed: {undo_err}") from err
        raise


def _restart_policy(container: Any, policy: str) -> None:
    try:
        container.update(restart_policy={"Name": policy})
    except Exception as err:
        raise PatchError(f"docker update --restart {policy} failed for {container.name}: {err}") from err


def _perform_undo(record: dict[str, str], client: Any) -> None:
    container = _find_recorded_container(client, record)
    if record["undo_action"] == "redis_aof_disable":
        _redis_command(container, enabled=False)
    elif record["undo_action"] == "restart_policy_no":
        _restart_policy(container, "no")
    else:  # State validation should make this unreachable.
        raise PatchError(f"unknown undo action '{record['undo_action']}'")


def _make_undo(record: dict[str, str], client: Any) -> Callable[[], None]:
    def undo() -> None:
        _perform_undo(record, client)
        _remove_record(record["id"])
        guard.unregister(undo)
        _REGISTERED.pop(record["id"], None)

    undo.__name__ = f"undo_{record['fix']}_{record['id']}"
    return undo


def apply(pat: Any) -> None:
    """Validate and apply each supported patch, registering an undo with safety.guard."""
    patches = _validate_patch(pat)
    with _STATE_LOCK:
        existing = _read_state()
    existing_ids = {record["id"] for record in existing}
    existing_targets = {(record["fix"], record["service"]) for record in existing}
    for item in patches:
        if item["id"] in existing_ids:
            raise PatchError(f"patch id '{item['id']}' is already applied or pending revert")
        if (item["fix"], item["service"]) in existing_targets:
            raise PatchError(f"fix '{item['fix']}' for service '{item['service']}' is already applied")

    client = _docker_client()
    containers = {item["id"]: _find_service(client, item["service"]) for item in patches}
    for item in patches:
        container = containers[item["id"]]
        if item["fix"] == "redis_aof" and getattr(container, "status", None) != "running":
            raise PatchError(f"Redis container {container.name} is not running")

    for item in patches:
        container = containers[item["id"]]
        record = {
            "id": item["id"],
            "experiment": item["experiment"],
            "service": item["service"],
            "fix": item["fix"],
            "container_id": str(container.id),
            "container_name": str(container.name),
            "undo_action": UNDO_ACTIONS[item["fix"]],
            "status": "applying",
        }
        _save_record(record)
        try:
            if item["fix"] == "redis_aof":
                _redis_command(container, enabled=True)
            else:
                _restart_policy(container, "unless-stopped")
        except Exception as err:
            try:
                _remove_record(item["id"])
            except Exception as state_err:
                raise PatchError(
                    f"{err}; failed to clear unapplied intent state: {state_err}"
                ) from err
            raise

        undo = _make_undo(record, client)
        try:
            # Register immediately after the live mutation succeeds.
            guard.register(undo, info=dict(record))
        except Exception as err:
            try:
                undo()
            except Exception as undo_err:
                raise PatchError(
                    f"patch '{item['id']}' applied but undo registration failed ({err}); "
                    f"immediate rollback failed ({undo_err}); state retained for recovery"
                ) from err
            raise PatchError(f"undo registration failed; patch '{item['id']}' was rolled back: {err}") from err
        _REGISTERED[item["id"]] = undo
        try:
            _update_status(item["id"], "applied")
        except Exception as err:
            raise PatchError(
                f"patch '{item['id']}' was applied and registered, but state could not be finalized: {err}"
            ) from err


def revert() -> None:
    """Undo recorded fixes in reverse order; retain every record whose undo fails."""
    with _STATE_LOCK:
        records = list(_read_state())
    if not records:
        print("No applied fixes to revert.")
        return
    client = _docker_client()
    failures: list[str] = []
    reverted: list[str] = []
    for record in reversed(records):
        undo = _REGISTERED.get(record["id"]) or _make_undo(record, client)
        try:
            undo()
        except Exception as err:
            failures.append(f"{record['id']} ({record['service']}/{record['fix']}): {err}")
        else:
            reverted.append(record["id"])
    if reverted:
        print("Reverted fixes: " + ", ".join(reverted))
    if failures:
        raise PatchError("one or more fixes could not be reverted: " + "; ".join(failures))


def _load_patch(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as patch_file:
            return json.load(patch_file)
    except (OSError, json.JSONDecodeError) as err:
        raise PatchError(f"cannot load patch file '{path}': {err}") from err


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or revert live Robot Shop resilience fixes")
    commands = parser.add_subparsers(dest="command", required=True)
    apply_parser = commands.add_parser("apply", help="validate and apply a patch.json file")
    apply_parser.add_argument("patch_file")
    commands.add_parser("revert", help="revert recorded applied fixes")
    try:
        args = parser.parse_args(argv)
        if args.command == "apply":
            apply(_load_patch(args.patch_file))
            print("Applied all patches successfully.")
        else:
            revert()
        return 0
    except PatchError as err:
        print(f"FAIL: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
