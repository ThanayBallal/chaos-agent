"""P5: safety controller. The planner proposes, the guard decides."""
import atexit
import json
import signal
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from safety.allowlist import (ERR_HOLD_S, ERR_MAX, FAULTS, LBL, MAX_DUR, P95_MAX, PRJ,
                              READY_ERR_FLOOR, READY_ERR_MULT, SVC_LBL)

ROT = Path(__file__).resolve().parent.parent
STP = ROT / "STOP"
ACT = ROT / "data" / "active_faults.json"
_rev = []  # [(revert_fn, info, registered_at)]
_brc = {"since": None}
_ctx = {"target": None, "blast": None}
_ins = {"atexit": False}


def blast_radius(dep, tgt):
    """tgt plus every service that transitively depends on it (from depends_on and edges)."""
    rdp = {}
    for svc in dep.get("services", []):
        for dst in svc.get("depends_on", []):
            rdp.setdefault(dst, set()).add(svc["name"])
    for edg in dep.get("edges", []):
        rdp.setdefault(edg["dst"], set()).add(edg["src"])
    out, que = {tgt}, deque([tgt])
    while que:
        for src in rdp.get(que.popleft(), ()):
            if src not in out:
                out.add(src)
                que.append(src)
    return out


def _container_exists(tgt):
    import docker

    cli = docker.from_env()
    return bool(cli.containers.list(all=True, filters={"label": [f"{LBL}={PRJ}", f"{SVC_LBL}={tgt}"]}))


def approve(exp, dep, mock=False):
    """Return None if exp may run, else a rejection reason. mock=True skips the live Docker check."""
    fal = exp.get("fault")
    if fal not in FAULTS:
        return f"fault '{fal}' not in allowlist"
    tgt = exp.get("target")
    nam = {svc["name"] for svc in dep["services"]}
    if tgt not in nam:
        return f"target '{tgt}' not in project"
    prm = exp.get("params", {})
    if set(prm) != FAULTS[fal]:
        return f"params {sorted(prm)} != {sorted(FAULTS[fal])}"
    if not 0 < prm["duration_s"] <= MAX_DUR:
        return f"duration {prm['duration_s']}s outside 1..{MAX_DUR}"
    if not mock:
        try:
            if not _container_exists(tgt):
                return f"no container for '{tgt}' in compose project {PRJ}"
        except Exception as err:
            return f"docker check failed: {err}"
    _ctx.update(target=tgt, blast=blast_radius(dep, tgt))
    return None


def check(smp, bas, tgt=None, allowed=None):
    """smp = {"t": secs_since_start, "error_rate", "p95_ms", "health": {svc: state}}. Return abort reason or None.
    allowed = services that may go unhealthy (blast radius); defaults to the radius from the last approve()
    for this target, else just {tgt}."""
    if kill_switch():
        return "kill switch (STOP file)"
    if smp["p95_ms"] > P95_MAX:
        return f"p95 {smp['p95_ms']}ms > {P95_MAX}ms"
    if smp["error_rate"] > ERR_MAX:
        _brc["since"] = smp["t"] if _brc["since"] is None else _brc["since"]
        if smp["t"] - _brc["since"] >= ERR_HOLD_S:
            return f"error_rate > {ERR_MAX} for {ERR_HOLD_S}s"
    else:
        _brc["since"] = None
    if allowed is None:
        allowed = _ctx["blast"] if tgt is not None and _ctx["target"] == tgt else {tgt}
    bad = [f"{svc}={sta}" for svc, sta in _states(smp, bas).items() if svc not in allowed and sta != "healthy"]
    if bad:
        return f"collateral unhealthy outside blast radius: {', '.join(bad)}"
    return None


def _states(smp, bas):
    """Sample health plus 'missing' for every baseline service absent from the sample."""
    hlt = dict(smp.get("health", {}))
    for svc in bas.get("health", {}):
        hlt.setdefault(svc, "missing")
    return hlt


def ready(smp, bas):
    """Return a reason if the system is not at steady state, else None. Any state but 'healthy' is bad."""
    bad = [f"{svc}={sta}" for svc, sta in _states(smp, bas).items() if sta != "healthy"]
    if bad:
        return f"not steady: unhealthy {', '.join(bad)}"
    lim = READY_ERR_MULT * max(bas.get("error_rate", 0), READY_ERR_FLOOR)
    if smp["error_rate"] > lim:
        return f"not steady: error_rate {smp['error_rate']} > {READY_ERR_MULT}x baseline ({lim:.4f})"
    return None


def reset():
    """Clear breach timers only. The blast radius is owned by approve()."""
    _brc["since"] = None


def kill_switch():
    return STP.exists()


def _save():
    try:
        ACT.parent.mkdir(parents=True, exist_ok=True)
        ACT.write_text(json.dumps(
            [{"name": getattr(fnc, "__qualname__", repr(fnc)), "info": inf, "registered_at": at}
             for fnc, inf, at in _rev], indent=2, default=str))
    except Exception as err:
        print(f"[guard] could not persist active faults: {err}")


def register(fnc, info=None):
    _rev.append((fnc, info, datetime.now(timezone.utc).isoformat()))
    _save()


def unregister(fnc):
    _rev[:] = [itm for itm in _rev if itm[0] is not fnc]
    _save()


def revert_all():
    if not _rev:
        return
    while _rev:
        fnc = _rev.pop()[0]
        try:
            fnc()
        except Exception as err:
            print(f"[guard] revert failed: {err}")
    _save()


def install():
    if STP.exists():
        STP.unlink()
        print("[guard] removed stale STOP file")

    def hdl(sig, frm):
        print("\n[guard] Ctrl+C - reverting all faults")
        revert_all()
        raise SystemExit(1)

    signal.signal(signal.SIGINT, hdl)
    if not _ins["atexit"]:
        atexit.register(revert_all)
        _ins["atexit"] = True
