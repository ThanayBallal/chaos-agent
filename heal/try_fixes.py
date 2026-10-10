"""Try the heal catalog on the live robot-shop, one fault at a time, and print a table.
python -m heal.try_fixes [--services redis ratings] [--faults kill pause] [--wait 45]

Per service x fault: guard.approve -> wait until every service is healthy -> injector.inject + guard.register ->
hold HOLD_S s -> graph.heal_plan actions via heal.fixes -> wait until every service is healthy (timed) ->
revert (always, so nothing is left behind) -> one row. STOP file or Ctrl+C reverts everything. Exit 0 if all healed."""
import argparse
import itertools
import json
import sys
import time

from heal import fixes, graph
from monitor import baseline, injector
from safety import guard
from safety.allowlist import FAULTS

HOLD_S = 2  # seconds the fault is left in place before healing
PRM = {"duration_s": 30, "delay_ms": 300, "cpus": 1}  # filtered to the fault's allowlisted params
DEP = graph.DAT / "depmap.json"
COLS = (("service", 9), ("fault", 6), ("during", 8), ("actions", 42), ("fix_ok", 6), ("healthy", 7), ("heal_s", 6))


def _depmap():
    if DEP.exists():
        return json.loads(DEP.read_text())
    return {"services": [{"name": svc} for svc in baseline.health()]}


def wait_healthy(sec):
    """Poll docker health until every robot-shop service is healthy or sec pass. Return (ok, unhealthy list)."""
    end = time.monotonic() + sec
    while True:
        hlt = baseline.health()
        bad = sorted(f"{svc}={sta}" for svc, sta in hlt.items() if sta != "healthy")
        if (hlt and not bad) or time.monotonic() >= end or guard.kill_switch():
            return bool(hlt) and not bad, bad or (["health unavailable"] if not hlt else [])
        time.sleep(0.5)


def trial(svc, fal, dep, wait):
    exp = {"id": f"t-{svc}-{fal}", "target": svc, "fault": fal, "params": {key: PRM[key] for key in FAULTS[fal]}}
    row = {"service": svc, "fault": fal, "during": "", "actions": "", "fix_ok": False, "healthy": False,
           "heal_s": None, "detail": ""}
    rej = guard.approve(exp, dep)
    if rej:
        row["detail"] = f"rejected: {rej}"
        return row
    ok, bad = wait_healthy(wait)
    if not ok:
        row["detail"] = f"not injected, not healthy: {', '.join(bad)}"
        return row
    rev = None
    try:
        rev = injector.inject(exp)
        guard.register(rev, info=exp)
        time.sleep(HOLD_S)
        con = injector.container(svc)
        con.reload()
        row["during"] = con.status
        t_0 = time.monotonic()
        res = [fixes.heal(itm["action"], itm["target"]) for itm in graph.heal_plan(fal, svc)]
        ok, bad = wait_healthy(wait)
        row.update(actions=" + ".join(f"{out['action']}({out['target']})" for out in res),
                   fix_ok=all(out["ok"] for out in res), healthy=ok,
                   heal_s=round(time.monotonic() - t_0, 1) if ok else None,
                   detail="; ".join(out["detail"] for out in res) + ("" if ok else f"; still unhealthy: {', '.join(bad)}"))
    except Exception as err:
        row["detail"] = f"{type(err).__name__}: {err}"
    finally:
        if rev is not None:
            try:
                rev()
                guard.unregister(rev)
            except Exception as err:
                row["detail"] += f"; REVERT FAILED: {err}"
    return row


def table(rows):
    print("  ".join(f"{nam:<{wid}}" for nam, wid in COLS) + "  detail")
    for row in rows:
        print("  ".join(f"{str(row[nam]):<{wid}}" for nam, wid in COLS) + f"  {row['detail'][:160]}")


def main(argv=None):
    arg = argparse.ArgumentParser(prog="python -m heal.try_fixes")
    arg.add_argument("--services", nargs="+", default=["redis", "ratings"])
    arg.add_argument("--faults", nargs="+", default=["kill", "pause"], choices=sorted(graph.HEAL))
    arg.add_argument("--wait", type=int, default=45, help="seconds to wait for all services healthy")
    opt = arg.parse_args(argv)
    guard.install()
    dep, rows = _depmap(), []
    try:
        for svc, fal in itertools.product(opt.services, opt.faults):
            if guard.kill_switch():
                print("[try_fixes] kill switch (STOP file): stopping")
                break
            print(f"[try_fixes] {fal} {svc} ...")
            rows.append(trial(svc, fal, dep, opt.wait))
    finally:
        guard.revert_all()
    table(rows)
    return 0 if rows and all(row["fix_ok"] and row["healthy"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
