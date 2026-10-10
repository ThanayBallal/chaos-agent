"""Heal catalog: undo one injected fault on a robot-shop container now, with the Docker SDK.

heal(action, target) -> {"action", "target", "ok", "detail"}; it never raises. Targets must be the single container
labelled com.docker.compose.project=robot-shop + com.docker.compose.service=<target>; anything else is refused.
Actions (== heal.graph.HEAL_CATALOG): start_service, unpause_service, remove_latency, stop_cpu_stress, restart_web.
"""
import time

import docker

from monitor import injector
from safety.allowlist import LBL, PRJ, SVC_LBL

WEB = "web"
WAIT_S = 20  # max seconds to wait for a container to report running
CPU_SIG = "while :; do :; done"  # body of injector._cpu's busy loops; their cmdline also holds injector.PID
TC_GONE = ("no such", "handle of zero")  # tc errors meaning there is no qdisc to delete

# Kill only the injector's busy loops. SIG/PIDF come in through the environment, so this script's own cmdline
# never contains them. Zombies have an empty cmdline, so they never match. Exit 1 if a loop survives.
CPU_SCAN = ('scan() { for d in /proc/[0-9]*; do p=${d#/proc/}; [ "$p" = "$$" ] && continue; '
            'c=$(tr "\\000" " " < "$d/cmdline" 2>/dev/null) || continue; '
            'case "$c" in *"$SIG"*) case "$c" in *"$PIDF"*) echo "$p";; esac;; esac; done; }; '
            'P=$(scan); for p in $P; do kill -9 "$p" 2>/dev/null; done; rm -f "$PIDF"; sleep 0.2; '
            'L=$(scan); if [ -n "$L" ]; then echo "alive" $L; exit 1; fi; echo "killed" $P')


def _out(action, target, ok, detail):
    return {"action": action, "target": target, "ok": bool(ok), "detail": str(detail)[:300]}


def container(target):
    """The one robot-shop container for compose service target; LookupError otherwise."""
    if not isinstance(target, str) or not target:
        raise LookupError(f"bad target {target!r}")
    cons = injector._docker().containers.list(all=True, filters={"label": [f"{LBL}={PRJ}", f"{SVC_LBL}={target}"]})
    cons = [con for con in cons if con.labels.get(LBL) == PRJ and con.labels.get(SVC_LBL) == target]
    if len(cons) != 1:
        raise LookupError(f"expected 1 container for service '{target}' in project {PRJ}, found {len(cons)}")
    return cons[0]


def _wait_running(con):
    end = time.monotonic() + WAIT_S
    while True:
        con.reload()
        if con.status == "running" or time.monotonic() >= end:
            return con.status == "running"
        time.sleep(0.5)


def start_service(con):
    con.reload()
    if con.status == "running":
        return True, "already running"
    if con.status == "paused":
        return False, "paused, not stopped: use unpause_service"
    was = con.status
    con.start()
    return (True, f"started (was {was})") if _wait_running(con) else (False, f"still {con.status} {WAIT_S}s after start")


def unpause_service(con):
    con.reload()
    if con.status != "paused":
        return con.status == "running", f"not paused ({con.status})"
    con.unpause()
    con.reload()
    return con.status == "running", f"unpaused, now {con.status}"


def has_netem(con):
    """True if con's DEV has a netem qdisc. Uses the helper's exit code: docker-py returns no output under some
    logging drivers, so `b"netem" in output` cannot be trusted."""
    try:
        injector._tc(con, f"tc qdisc show dev {injector.DEV} | grep -q netem")
    except docker.errors.ContainerError as err:
        if err.exit_status == 1:
            return False
        raise
    return True


def remove_latency(con):
    con.reload()
    if con.status != "running":
        return True, f"{con.status}: no netem (a restarted container gets a fresh network namespace)"
    try:
        injector._tc(con, f"tc qdisc del dev {injector.DEV} root")
        how = "deleted root qdisc"
    except docker.errors.ContainerError as err:
        msg = (err.stderr or b"").decode(errors="replace") if isinstance(err.stderr, bytes) else str(err.stderr or err)
        if not any(itm in msg.lower() for itm in TC_GONE):
            return False, f"tc qdisc del failed: {msg.strip()}"
        how = "no qdisc to delete"
    if has_netem(con):
        return False, f"{how}, but netem is still on {injector.DEV}"
    return True, f"{how}; no netem on {injector.DEV}"


def stop_cpu_stress(con):
    con.reload()
    if con.status == "paused":
        return False, "paused: cannot exec into it, use unpause_service first"
    if con.status != "running":
        return True, f"{con.status}: busy loops die with the container"
    res = con.exec_run(["sh", "-c", CPU_SCAN], environment={"SIG": CPU_SIG, "PIDF": injector.PID})
    txt = (res.output or b"").decode(errors="replace").strip()
    return res.exit_code == 0, txt or f"exit {res.exit_code}"


def restart_web(con):
    con.restart(timeout=5)
    return (True, "restarted") if _wait_running(con) else (False, f"{con.status} {WAIT_S}s after restart")


ACTIONS = {fnc.__name__: fnc for fnc in (start_service, unpause_service, remove_latency, stop_cpu_stress, restart_web)}


def heal(action, target):
    """Run one catalog action on one robot-shop service. Never raises."""
    if action not in ACTIONS:
        return _out(action, target, False, f"rejected: '{action}' not in heal catalog {sorted(ACTIONS)}")
    if action == "restart_web" and target != WEB:
        return _out(action, target, False, f"rejected: restart_web only targets '{WEB}'")
    try:
        con = container(target)
    except Exception as err:
        return _out(action, target, False, f"rejected: {err}")
    try:
        ok, det = ACTIONS[action](con)
    except Exception as err:
        ok, det = False, f"{type(err).__name__}: {err}"
    return _out(action, target, ok, det)
