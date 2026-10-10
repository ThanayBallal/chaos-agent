"""P3: fault injection on compose services. Every inject returns a revert function."""
import docker

PRJ = "robot-shop"
NET = "nicolaka/netshoot"  # helper image with tc, joins the target's network namespace
DEV = "eth0"
PID = "/tmp/.chaos_cpu"

_cli = {"docker": None}


def _docker():
    if _cli["docker"] is None:
        _cli["docker"] = docker.from_env()
    return _cli["docker"]


def container(svc):
    """Return the docker container for compose service svc in project robot-shop."""
    cons = _docker().containers.list(all=True, filters={"label": [
        f"com.docker.compose.project={PRJ}", f"com.docker.compose.service={svc}"]})
    if not cons:
        raise LookupError(f"no container for service '{svc}' in project {PRJ}")
    return cons[0]


def _once(fnc):
    """Make a revert idempotent: runner, guard and Ctrl+C may all call it.
    Marked done only after it succeeds, so a failed revert can be retried."""
    don = {"v": False}

    def rev():
        if don["v"]:
            return
        fnc()
        don["v"] = True

    return rev


def _helper(cmd, **kw):
    """Run a short-lived netshoot helper container (has tc, kill, sh) and return its output."""
    try:
        _docker().images.get(NET)
    except docker.errors.ImageNotFound:
        _docker().images.pull(NET)
    return _docker().containers.run(NET, cmd, remove=True, **kw)


def _tc(con, cmd):
    return _helper(["sh", "-c", cmd], network_mode=f"container:{con.id}", cap_add=["NET_ADMIN"])


def _kill(con, prm):
    """Crash the container: SIGKILL its main process, so it gets no chance to save state (unlike docker stop).
    Sent from a pid=host helper, Docker sees an unexpected exit, exactly like a real crash, so a restart
    policy (if any) applies. Fallback: docker kill, also SIGKILL, but Docker treats it as a manual stop
    and ignores restart policies."""
    pid = con.attrs.get("State", {}).get("Pid")
    how = None
    if pid:
        try:
            _helper(["kill", "-KILL", str(pid)], pid_mode="host")
            how = "crash: SIGKILL to main process (restart policy applies)"
        except Exception:
            how = None
    if how is None:
        con.kill(signal="SIGKILL")
        how = "docker kill SIGKILL (restart policy ignored by docker)"

    def rev():
        con.reload()
        if con.status != "running":
            con.start()
        con.reload()
        if con.status != "running":
            raise RuntimeError(f"{con.name} is {con.status} after restart")
    rev.info = how
    return rev


def _pause(con, prm):
    con.pause()

    def rev():
        con.reload()
        if con.status == "paused":
            con.unpause()
        con.reload()
        if con.status == "paused":
            raise RuntimeError(f"{con.name} still paused after unpause")
    rev.info = "docker pause (SIGSTOP via freezer cgroup)"
    return rev


# delete only if a netem qdisc exists (no "no such qdisc" error when it is already gone), then verify
NETEM_DEL = (f"if tc qdisc show dev {DEV} | grep -q netem; then tc qdisc del dev {DEV} root; fi; "
             f"if tc qdisc show dev {DEV} | grep -q netem; then echo still-there; exit 1; fi")


def _latency(con, prm):
    def rev():
        con.reload()
        if con.status != "running":
            return  # a stopped/restarted container gets a fresh network namespace without netem
        _tc(con, NETEM_DEL)

    try:
        # replace, not add: a qdisc left behind by a crashed earlier run must not block this one
        _tc(con, f"tc qdisc replace dev {DEV} root netem delay {int(prm['delay_ms'])}ms")
    except Exception:
        _cleanup(rev)
        raise
    rev.info = f"tc netem delay {int(prm['delay_ms'])}ms on {DEV}"
    return rev


# kill exactly the PIDs this inject started (file is recreated each time, so no stale PIDs), then verify.
# A killed loop may linger as a zombie (state Z) when PID 1 does not reap orphans: dead, uses no CPU, so it counts as gone.
CPU_KILL = (f"P=$(cat {PID} 2>/dev/null); for p in $P; do kill -9 $p 2>/dev/null; done; rm -f {PID}; sleep 0.2; "
            f"for p in $P; do if [ -r /proc/$p/stat ] && [ \"$(cut -d' ' -f3 /proc/$p/stat)\" != Z ]; then "
            f"echo alive $p; exit 1; fi; done")


def _cpu(con, prm):
    def rev():
        con.reload()
        if con.status != "running":
            return  # loops die with the container
        res = con.exec_run(["sh", "-c", CPU_KILL])
        if res.exit_code != 0:
            raise RuntimeError(f"cpu_stress loops still running: {res.output[:200]!r}")

    num = max(1, int(prm["cpus"]))
    scr = (f"rm -f {PID}; i=0; while [ $i -lt {num} ]; do (while :; do :; done) >/dev/null 2>&1 & "
           f"echo $! >> {PID}; i=$((i+1)); done")
    try:
        res = con.exec_run(["sh", "-c", scr])
        if res.exit_code != 0:
            raise RuntimeError(f"cpu_stress exec failed: {res.output[:200]!r}")
    except Exception:
        _cleanup(rev)  # some loops may have started before the failure
        raise
    rev.info = f"{num} busy loop(s) inside the container"
    return rev


def _cleanup(rev):
    """Best-effort undo of a half-applied fault; the original error is what gets raised."""
    try:
        rev()
    except Exception as err:
        print(f"[injector] cleanup after failed inject also failed: {err}")


FNS = {"kill": _kill, "pause": _pause, "latency": _latency, "cpu_stress": _cpu}


def inject(exp):
    """Apply exp (plan.json item). Return a zero-arg revert() that undoes it."""
    fal = exp["fault"]
    if fal not in FNS:
        raise ValueError(f"unsupported fault '{fal}'")
    con = container(exp["target"])
    if con.status != "running":
        raise RuntimeError(f"target '{exp['target']}' is {con.status}, not running; refusing to inject")
    raw = FNS[fal](con, exp.get("params", {}))
    rev = _once(raw)
    rev.info = getattr(raw, "info", "")  # how the fault was applied, e.g. which kill method
    return rev
