"""P5: safety controller. The planner proposes, the guard decides."""
import signal
from pathlib import Path

from safety.allowlist import ERR_HOLD_S, ERR_MAX, FAULTS, MAX_DUR, P95_MAX

STP = Path(__file__).resolve().parent.parent / "STOP"
_rev = []
_brc = {"since": None}


def approve(exp, dep):
    """Return None if exp may run, else a rejection reason."""
    fal = exp.get("fault")
    if fal not in FAULTS:
        return f"fault '{fal}' not in allowlist"
    nam = {svc["name"] for svc in dep["services"]}
    if exp.get("target") not in nam:
        return f"target '{exp.get('target')}' not in project"
    prm = exp.get("params", {})
    if set(prm) != FAULTS[fal]:
        return f"params {sorted(prm)} != {sorted(FAULTS[fal])}"
    if not 0 < prm["duration_s"] <= MAX_DUR:
        return f"duration {prm['duration_s']}s outside 1..{MAX_DUR}"
    return None


def check(smp, bas, tgt=None):
    """smp = {"t": secs_since_start, "error_rate", "p95_ms", "health": {svc: state}}. Return abort reason or None."""
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
    bad = [svc for svc, sta in smp.get("health", {}).items() if svc != tgt and sta == "unhealthy"]
    if bad:
        return f"collateral unhealthy: {', '.join(bad)}"
    return None


def kill_switch():
    return STP.exists()


def register(fnc):
    _rev.append(fnc)


def revert_all():
    while _rev:
        try:
            _rev.pop()()
        except Exception as err:
            print(f"[guard] revert failed: {err}")


def install():
    def hdl(sig, frm):
        print("\n[guard] Ctrl+C - reverting all faults")
        revert_all()
        raise SystemExit(1)

    signal.signal(signal.SIGINT, hdl)
