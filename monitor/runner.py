"""P3: run one experiment end to end."""
import inspect
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from monitor import baseline, injector

REC_MAX = 120  # give up measuring recovery after this many seconds
REC_OK = 3  # consecutive in-band samples needed to call it recovered
OUT = Path(__file__).resolve().parent.parent / "data" / "results" / "result.json"
RDY_MULT, RDY_FLOOR = 3, 0.01  # steady-state fallback for a guard without ready(); same limits as P5's


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ready(grd, smp, bas):
    """Steady-state check before injecting. Uses grd.ready() when the guard has it."""
    if hasattr(grd, "ready"):
        return grd.ready(smp, bas)
    bad = [svc for svc, sta in smp["health"].items() if sta == "unhealthy"]
    if bad:
        return f"not steady: unhealthy {', '.join(bad)}"
    lim = RDY_MULT * max(bas["error_rate"], RDY_FLOOR)
    if smp["error_rate"] > lim:
        return f"not steady: error_rate {smp['error_rate']} > {lim:.4f}"
    return None


def _register(grd, rev, exp):
    """register(fnc) on the current guard, register(fnc, info) on P5's newer one."""
    if len(inspect.signature(grd.register).parameters) > 1:
        grd.register(rev, exp)
    else:
        grd.register(rev)


def _save(res):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2))


def _in_band(smp, bas, tgt):
    """True if a post-revert sample is back near baseline and the target is healthy again."""
    err_ok = smp["error_rate"] <= max(2 * bas["error_rate"], bas["error_rate"] + 0.02)
    p95_ok = smp["p95_ms"] <= max(2 * bas["p95_ms"], bas["p95_ms"] + 100)
    return err_ok and p95_ok and smp["health"].get(tgt, "healthy") == "healthy"


def _observe(smps, agg, bas, tgt, abr, rec, rnm, err):
    """Build observations from measured samples only."""
    obs = []
    if not smps:
        return [err] if err else ["no samples taken"]
    obs.append(f"error_rate {bas['error_rate']} baseline -> {agg['error_rate']} for requests sent during fault "
               f"({agg['errors']}/{agg['requests']} failed, final outcomes)")
    obs.append(f"p95 {bas['p95_ms']}ms baseline -> {agg['p95_ms']}ms during fault")
    obs.append(f"{len(smps)} samples, last at t={smps[-1]['t']}s")
    for nam, eps in agg["endpoints"].items():
        fst = next((s["t"] for s in smps if s["endpoints"][nam]["errors"]), None)
        txt = f"{nam}: {eps['errors']}/{eps['requests']} failed"
        if eps["timeouts"]:
            txt += f" ({eps['timeouts']} timed out: no response within {baseline.TMO:g}s)"
        if eps["pending"]:
            txt += f", {eps['pending']} still had no outcome {baseline.TMO:g}s after revert"
        if eps["dropped"]:
            txt += f", {eps['dropped']} not sent (load generator in-flight cap reached)"
        txt += f", p95 {eps['p95_ms']}ms"
        if fst is not None:
            txt += f", first error in sample ending t={fst}s"
        elif eps["errors"]:
            txt += ", all failures arrived after the last fault sample"
        else:
            txt += ", no errors"
        obs.append(txt)
    seen = {}
    for smp in smps:
        for svc, sta in smp["health"].items():
            if sta != "healthy" and svc not in seen:
                seen[svc] = (sta, smp["t"])
    for svc, (sta, t) in seen.items():
        obs.append(f"{svc} first seen {sta} in sample ending t={t}s" + (" (target)" if svc == tgt else ""))
    if abr:
        obs.append(f"aborted at t={smps[-1]['t']}s: {abr}")
    if err:
        obs.append(err)
    obs.append(rnm if rec is None else f"returned to baseline band {rec}s after revert")
    return obs


def _recover(bas, tgt, grd):
    """Sample each 1s after revert. Return (seconds until REC_OK in-band samples in a row began, None)
    or (None, why not measured)."""
    t0 = time.monotonic()
    run, fst = 0, None
    while time.monotonic() - t0 < REC_MAX:
        if grd.kill_switch():
            return None, "recovery not measured: kill switch"
        t = time.monotonic() - t0
        if _in_band(baseline.sample(1.0), bas, tgt):
            fst = t if run == 0 else fst
            run += 1
            if run >= REC_OK:
                return round(fst), None
        else:
            run = 0
    return None, f"did not return to baseline band within {REC_MAX}s after revert"


def run(exp, bas, grd):
    """inject -> guard.register(revert) -> sample each 1s -> grd.check(smp, bas, exp['target'])
    -> revert on timeout or abort -> measure recovery_s -> return dict matching result.json."""
    tgt, dur = exp["target"], exp["params"]["duration_s"]
    sta = _now()
    smps, raw, abr, err, rec, rnm, rev = [], [], None, None, None, None, None
    if hasattr(grd, "reset"):
        grd.reset()
    pre = baseline.sample(1.0)
    nrd = _ready(grd, pre, bas)
    if nrd:
        err = f"skipped, not injected: {nrd}"
    else:
        try:
            rev = injector.inject(exp)
        except Exception as exc:
            err = f"injection failed: {type(exc).__name__}: {exc}"
    if rev:
        _register(grd, rev, exp)
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < dur:
                smp = baseline.sample(1.0)
                smp["t"] = round(time.monotonic() - t0)
                smps.append(smp)
                abr = grd.check(smp, bas, tgt)
                if abr:
                    break
        finally:
            t_rev = time.monotonic()
            try:
                rev()
                if hasattr(grd, "unregister"):
                    grd.unregister(rev)
            except Exception as exc:
                err = f"revert failed: {type(exc).__name__}: {exc}"
        rec, rnm = _recover(bas, tgt, grd)
        # let requests sent during the fault reach their final outcome (answer or timeout)
        while time.monotonic() < t_rev + baseline.TMO + 0.5 and not grd.kill_switch():
            time.sleep(0.2)
        raw = baseline.sent_between(t0, t_rev)
    agg = baseline.stats(raw)  # every request sent while the fault was active, with its final outcome
    res = {"id": exp["id"], "target": tgt, "fault": exp["fault"], "started_at": sta, "ended_at": _now(),
           "error_rate": agg["error_rate"] if raw else None, "p95_ms": agg["p95_ms"] if raw else None,
           "recovery_s": rec, "aborted": bool(abr or err), "abort_reason": abr or err,
           "observations": _observe(smps, agg, bas, tgt, abr, rec, rnm, err)}
    _save(res)
    return res


def main():
    """python -m monitor.runner <fault> <target> [duration_s] [delay_ms|cpus] [baseline_s]"""
    from safety import guard
    from safety.allowlist import FAULTS

    arg = sys.argv[1:]
    fal, tgt = arg[0], arg[1]
    dur = int(arg[2]) if len(arg) > 2 else 30
    prm = {"duration_s": dur}
    if fal == "latency":
        prm["delay_ms"] = int(arg[3]) if len(arg) > 3 else 500
    if fal == "cpu_stress":
        prm["cpus"] = int(arg[3]) if len(arg) > 3 else 1
    win = int(arg[4]) if len(arg) > 4 else 30
    exp = {"id": "t1", "target": tgt, "fault": fal, "params": prm}
    rej = guard.approve(exp, {"services": [{"name": svc} for svc in baseline.health()]})
    if rej or set(prm) != FAULTS.get(fal, set()):
        sys.exit(f"[guard] rejected: {rej}")
    guard.install()
    print(f"capturing {win}s baseline...")
    bas = baseline.capture(win)
    print(json.dumps(bas, indent=2))
    print(f"running {fal} on {tgt} for {dur}s...")
    res = run(exp, bas, guard)
    guard.revert_all()
    baseline.stop()
    print(json.dumps(res, indent=2))
    print(f"saved {OUT}")


if __name__ == "__main__":
    main()
