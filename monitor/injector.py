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
    """Make a revert idempotent: runner, guard and Ctrl+C may all call it."""
    don = {"v": False}

    def rev():
        if don["v"]:
            return
        don["v"] = True
        fnc()

    return rev


def _tc(con, cmd):
    _docker().containers.run(NET, ["tc", *cmd], network_mode=f"container:{con.id}",
                             cap_add=["NET_ADMIN"], remove=True)


# kill and pause are single docker API calls: if they raise, nothing was changed.
def _kill(con, prm):
    con.kill()

    def rev():
        con.reload()
        if con.status != "running":
            con.start()
    return rev


def _pause(con, prm):
    con.pause()

    def rev():
        con.reload()
        if con.status == "paused":
            con.unpause()
    return rev


def _latency(con, prm):
    def rev():
        con.reload()
        if con.status == "running":
            _tc(con, ["qdisc", "del", "dev", DEV, "root"])

    try:
        _docker().images.get(NET)
    except docker.errors.ImageNotFound:
        _docker().images.pull(NET)
    try:
        # replace, not add: a qdisc left behind by a crashed earlier run must not block this one
        _tc(con, ["qdisc", "replace", "dev", DEV, "root", "netem", "delay", f"{int(prm['delay_ms'])}ms"])
    except Exception:
        _cleanup(rev)
        raise
    return rev


def _cpu(con, prm):
    def rev():
        con.reload()
        if con.status == "running":
            con.exec_run(["sh", "-c", f"kill $(cat {PID}) 2>/dev/null; rm -f {PID}"])

    num = max(1, int(prm["cpus"]))
    scr = (f"i=0; while [ $i -lt {num} ]; do (while :; do :; done) >/dev/null 2>&1 & "
           f"echo $! >> {PID}; i=$((i+1)); done")
    try:
        res = con.exec_run(["sh", "-c", scr])
        if res.exit_code != 0:
            raise RuntimeError(f"cpu_stress exec failed: {res.output[:200]!r}")
    except Exception:
        _cleanup(rev)  # some loops may have started before the failure
        raise
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
    return _once(FNS[fal](con, exp.get("params", {})))
