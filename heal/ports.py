"""The calls the heal nodes make into team modules, with their exact signatures.

Two modes. STUB (default, tests and --stub): each port logs and returns contract-shaped fake data marked "stub": True.
LIVE (use_live(True), heal.run --live): each port calls the real module below, adapted here when its shape differs.

  review       -> agent.code_review.review(dep)
  heal_fix     -> heal.fixes.heal(action, target) -> {action, target, ok, detail}
  make         -> agent.patcher.make(fnd, pln, dep, rsl=rsl, exclude=exclude or (), save=False)
                  (save=False: the heal loop must not overwrite the main pipeline's data/patch.json)
  apply        -> setup.patch.apply(pat): it accepts only the patches/code_suggestions keys and refuses an empty
                  patch list, so extra keys are stripped and an empty list is skipped here
  revert_patch -> setup.patch.revert()
  compare      -> monitor.compare.compare(..., pat=<last applied patch>) so patch_ids are filled in
  revert_all   -> injected faults only. guard.revert_all() would also run the patch undos that setup.patch.apply
                  registers with the guard, so this runs only the reverts that inject() registered (and unregisters
                  them). Patch undos stay with the guard for Ctrl+C / exit.
Live-only ports (raise outside live mode):
  approve      -> safety.guard.approve(exp, dep)
  inject       -> monitor.injector.inject(exp) + guard.register(revert), inside try
  observe      -> measured symptoms; knows NOTHING about the experiment
  verify       -> guard.ready on 1 s samples until steady (runner.REC_OK in a row) + recovery timing
states       -> {service: container status} of project robot-shop (console status lines during the hold; stub: fake)
heal_check works in both modes: the live heal guard (deadline, kill switch, collateral; no error-rate/p95 abort).
ready        -> guard.ready on one 1 s sample (stub: always steady); heal.campaign waits on it between cycles.
Campaign (heal.campaign, campaign(k) .. campaign(None)): hardening patches still applied at the end of a cycle are
kept (keep()) for the later cycles: make() excludes them and prefixes new patch ids with "c<k>-" (setup.patch refuses
a duplicate id or fix), and revert_patch() re-applies them after setup.patch.revert() (which undoes every patch).
"""
import time
from concurrent.futures import ThreadPoolExecutor

from safety import guard
from safety.allowlist import LBL, PRJ, SVC_LBL

REAL = {"review": "agent.code_review.review", "heal_fix": "heal.fixes.heal", "make": "agent.patcher.make",
        "apply": "setup.patch.apply", "revert_patch": "setup.patch.revert", "compare": "monitor.compare.compare",
        "revert_all": "safety.guard.revert_all", "approve": "safety.guard.approve",
        "inject": "monitor.injector.inject", "observe": "monitor.baseline.sample", "verify": "safety.guard.ready"}
FIXES = {"redis": ("redis_aof", "restart_policy")}  # stub preference order per service; others get restart_policy
OBS_S = 3  # seconds of traffic per observation
LOG_N, LOG_W = 15, 200  # log lines per unhealthy service, characters per line
MODE = {"live": False}
_faults = []  # revert functions registered by inject(), newest last
_last = {"pat": None}  # last patch applied live, for compare's patch_ids
_camp = {"cycle": None, "kept": [], "applied": []}  # heal.campaign: patches kept from earlier cycles / applied in this one


def use_live(on=True):
    MODE["live"] = bool(on)
    _last["pat"] = None


def live():
    return MODE["live"]


def _need_live(nam):
    if not MODE["live"]:
        raise RuntimeError(f"ports.{nam} is live-only; run heal.run with --live")


def log(nam, msg):
    print(f"[heal.ports] {nam}: {msg}" + ("" if MODE["live"] else " (stub)"))


def campaign(cycle):
    """heal.campaign: cycle `cycle` starts (its applied patches are tracked); None ends the campaign."""
    if cycle is None:
        _camp.update(cycle=None, kept=[], applied=[])
    else:
        _camp.update(cycle=cycle, applied=[])


def keep():
    """End of a campaign cycle: the patches it left applied stay applied for later cycles. -> those patches"""
    new, _camp["applied"] = _camp["applied"], []
    _camp["kept"] = _camp["kept"] + new
    return list(new)


def kept():
    """Patches kept applied from earlier campaign cycles."""
    return list(_camp["kept"])


def _tag(pat):
    """Campaign: unique patch ids across cycles (c<k>-p1, ...)."""
    if not _camp["cycle"]:
        return pat
    return {**pat, "patches": [{**itm, "id": f"c{_camp['cycle']}-{itm['id']}"} for itm in pat.get("patches") or []]}


def review(dep):
    """agent.code_review.review(dep) -> {"source", "files_read", "services", "risks": [...]}"""
    if MODE["live"]:
        from agent import code_review
        return code_review.review(dep)
    nam = {svc["name"] for svc in dep.get("services", [])}
    rsk = [{"service": "cart", "dependency": "redis", "file": "cart/server.js", "line": 0, "quote": "STUB",
            "weakness": "STUB: cart keeps its only copy of user state in redis", "fault": "kill",
            "params": {"duration_s": 30}, "hypothesis": "carts are lost after redis restarts",
            "fix": "give redis a volume with AOF persistence", "weight": 3}] if "redis" in nam else []
    log("review", f"{len(rsk)} risks")
    return {"source": "stub", "files_read": 0, "services": sorted(nam), "risks": rsk, "stub": True}


def heal_fix(action, target):
    """Undo the injected fault on target now (start_service, unpause_service, ..., restart_web).
    -> {"action", "target", "ok", "detail"}"""
    if MODE["live"]:
        from heal import fixes
        res = fixes.heal(action, target)
        log("heal_fix", f"{action}({target}) ok={res['ok']}: {res['detail']}")
        return res
    log("heal_fix", f"{action}({target})")
    return {"action": action, "target": target, "ok": True, "detail": "stub"}


def make(fnd, pln, dep, rsl=None, exclude=None):
    """agent.patcher.make(fnd, pln, dep, rsl=rsl, exclude=exclude) -> patch.json {"patches", "code_suggestions"}.
    rsl = results the findings came from; exclude = patch dicts already tried (round 1), never proposed again."""
    if _camp["cycle"]:
        exclude = list(exclude or ()) + _camp["kept"]
    if MODE["live"]:
        from agent import patcher
        return _tag(patcher.make(fnd, pln, dep, rsl=rsl, exclude=() if exclude is None else exclude, save=False))
    pex = {exp["id"]: exp for exp in pln}
    don = {(itm["fix"], itm["service"]) for itm in exclude or []}
    pts = []
    for itm in fnd.get("findings", []):
        tgt = pex.get(itm["experiment"], {}).get("target", itm["target"])
        fix = next((fix for fix in FIXES.get(tgt, ("restart_policy",)) if (fix, tgt) not in don), None)
        if fix:
            pts.append({"id": f"p{len(don) + len(pts) + 1}", "experiment": itm["experiment"], "service": tgt,
                        "fix": fix, "reason": f"STUB: {itm['weakness']}", "expected": f"{tgt} survives the fault"})
    log("make", f"{len(pts)} patches from {len(fnd.get('findings', []))} findings, {len(rsl or [])} results, "
                f"{len(don)} excluded")
    return _tag({"patches": pts, "code_suggestions": [], "stub": True})


def apply(pat):
    """setup.patch.apply(pat) -> None. Applies the patches live."""
    if MODE["live"]:
        pch = {"patches": list(pat.get("patches") or []), "code_suggestions": list(pat.get("code_suggestions") or [])}
        if not pch["patches"]:
            log("apply", "no patches to apply")
            return
        from setup import patch
        patch.apply(pch)
        _last["pat"] = pch
        _applied(pch["patches"])
        log("apply", ", ".join(itm["fix"] + " " + itm["service"] for itm in pch["patches"]))
        return
    _applied(pat.get("patches") or [])
    log("apply", f"{', '.join(itm['fix'] + ' ' + itm['service'] for itm in pat.get('patches', [])) or 'nothing'}")


def _applied(pts):
    if _camp["cycle"]:
        _camp["applied"] = _camp["applied"] + [dict(itm) for itm in pts]


def revert_patch():
    """setup.patch.revert() -> None. Undoes the applied patches; only used between hardening round 1 and 2."""
    _camp["applied"] = []
    if MODE["live"]:
        from setup import patch
        patch.revert()
        _last["pat"] = None
        _reapply(patch)
        return
    log("revert_patch", "round-1 patches undone" + (f", kept {', '.join(itm['id'] for itm in _camp['kept'])}"
                                                   if _camp["kept"] else ""))


def _reapply(patch):
    """Campaign: setup.patch.revert() undid the earlier cycles' patches too; apply them again one by one (a patch
    that cannot be re-applied is dropped from the kept list and logged)."""
    oks = []
    for itm in _camp["kept"]:
        try:
            patch.apply({"patches": [itm], "code_suggestions": []})
            oks.append(itm)
        except Exception as err:
            log("revert_patch", f"could not re-apply kept patch {itm['id']} ({itm['fix']} {itm['service']}): {err}")
    if oks:
        log("revert_patch", f"re-applied kept patches {', '.join(itm['id'] for itm in oks)}")
    _camp["kept"] = oks


def compare(rsl_before, rsl_after, fnd_before, fnd_after):
    """monitor.compare.compare(...) -> comparison.json {"patch_ids", "score", "experiments": [...]}.
    Stub "improved" is simply error_rate going down; the contract rule lives in monitor/compare.py."""
    if MODE["live"]:
        from monitor import compare as cmp_mod
        return cmp_mod.compare(rsl_before, rsl_after, fnd_before, fnd_after, pat=_last["pat"])
    aft = {res["id"]: res for res in rsl_after}
    exs = [{"id": bef["id"], "target": bef["target"], "fault": bef["fault"],
            "improved": aft[bef["id"]].get("error_rate", 1) < bef.get("error_rate", 0),
            **{key: [bef.get(key), aft[bef["id"]].get(key)]
               for key in ("error_rate", "p95_ms", "recovery_s", "data_survived")}}
           for bef in rsl_before if bef["id"] in aft]
    cmp = {"patch_ids": [], "score": {"before": fnd_before.get("resilience_score"),
                                      "after": fnd_after.get("resilience_score")},
           "experiments": exs, "stub": True}
    log("compare", f"score {cmp['score']['before']} -> {cmp['score']['after']}, "
                   f"improved={any(exp['improved'] for exp in exs)}")
    return cmp


def revert_all():
    """Revert every injected fault -> None. Patches are never reverted here (see module docstring)."""
    if not MODE["live"]:
        log("revert_all", "no registered faults")
        return
    num = 0
    while _faults:
        rev = _faults.pop()
        try:
            rev()
        except Exception as err:
            print(f"[heal.ports] revert_all: revert failed, left registered with the guard: {err}")
            continue
        guard.unregister(rev)
        num += 1
    if num:
        log("revert_all", f"{num} injected fault(s) reverted")


# ---- live-only ports ----

def approve(exp, dep):
    """safety.guard.approve(exp, dep) -> None or rejection reason (allowlist, depmap, params, live container)."""
    _need_live("approve")
    return guard.approve(exp, dep)


def inject(exp):
    """injector.inject(exp) + guard.register(revert). -> {"injected": True, "info"} or {"injected": False, "error"}.
    A fault that was applied but could not be registered is reverted at once."""
    _need_live("inject")
    from monitor import injector
    rev, reg = None, False
    try:
        rev = injector.inject(exp)
        guard.register(rev, info={key: exp.get(key) for key in ("id", "target", "fault", "params")})
        reg = True
        _faults.append(rev)
    except BaseException as err:
        if rev is not None:
            try:
                rev()
                if reg:
                    guard.unregister(rev)
            except Exception as rer:
                print(f"[heal.ports] inject: revert after failure also failed: {rer}")
        if not isinstance(err, Exception):
            raise
        return {"injected": False, "error": f"{type(err).__name__}: {err}"[:300]}
    return {"injected": True, "info": rev.info}


def _try(fnc, *arg):
    try:
        return fnc(*arg)
    except Exception:
        return None


def _tail(con):
    try:
        txt = con.logs(tail=LOG_N).decode(errors="replace")
    except Exception as err:
        return [f"logs unavailable: {type(err).__name__}"]
    return [lin[:LOG_W] for lin in txt.splitlines()[-LOG_N:]]


def _containers(cli):
    """{service: container} of project robot-shop, stopped ones included."""
    return {con.labels.get(SVC_LBL, con.name): con
            for con in cli.containers.list(all=True, filters={"label": f"{LBL}={PRJ}"})}


def states():
    """{service: container status} of project robot-shop. Console only (the hold's status line), never a symptom."""
    if MODE["live"]:
        from monitor import injector
        return {svc: con.status for svc, con in _containers(injector._docker()).items()}
    return {"web": "running", "cart": "running", "redis": "running"}


def observe(bas):
    """Measured symptoms of the whole app, with no knowledge of the experiment: OBS_S s of traffic (error_rate, p95,
    per endpoint), docker health, container status, cpu % (live_check) and netem (fixes.has_netem) per running container, last LOG_N log lines of unhealthy services."""
    _need_live("observe")
    from heal import fixes
    from monitor import baseline, injector, live_check
    smp = baseline.sample(OBS_S)
    hlt = smp["health"]
    bad = sorted(svc for svc, sta in hlt.items() if sta != "healthy")
    cli = injector._docker()
    cons = _containers(cli)
    run = {svc: con for svc, con in cons.items() if con.status == "running"}
    try:
        cli.images.get(injector.NET)
        hav = True
    except Exception:
        hav = False  # no helper image means nothing could have added netem
    with ThreadPoolExecutor(max_workers=8) as pool:
        cpu = dict(zip(run, pool.map(lambda con: _try(live_check.cpu_pct, con), run.values())))
        net = dict(zip(run, pool.map(lambda con: _try(fixes.has_netem, con), run.values()))) if hav else {}
    logs = {svc: _tail(cons[svc]) for svc in bad if svc in cons}
    obs = [f"error_rate {bas.get('error_rate')} baseline -> {smp['error_rate']} over {OBS_S}s "
           f"({smp['errors']}/{smp['requests']} failed)",
           f"p95 {bas.get('p95_ms')}ms baseline -> {smp['p95_ms']}ms"]
    obs += [f"{nam}: {eps['errors']}/{eps['requests']} failed, p95 {eps['p95_ms']}ms"
            for nam, eps in smp["endpoints"].items() if eps["errors"]]
    obs += [f"{svc} {hlt[svc]} (container {cons[svc].status})" if svc in cons else f"{svc} {hlt[svc]}" for svc in bad]
    obs += [f"{svc} cpu {val}% of one core" for svc, val in sorted(cpu.items())
            if val is not None and val >= live_check.CPU_BUSY]
    obs += [f"{svc} has a netem qdisc on {injector.DEV}" for svc, val in sorted(net.items()) if val]
    if not bad:
        obs.append("all services healthy")
    log("observe", f"error_rate={smp['error_rate']} p95={smp['p95_ms']}ms unhealthy={bad or 'none'}")
    return {"window_s": OBS_S, "requests": smp["requests"], "errors": smp["errors"], "error_rate": smp["error_rate"],
            "p50_ms": smp["p50_ms"], "p95_ms": smp["p95_ms"], "endpoints": smp["endpoints"], "health": hlt,
            "unhealthy": bad, "containers": {svc: con.status for svc, con in sorted(cons.items())}, "cpu_pct": cpu,
            "netem": sorted(svc for svc, val in net.items() if val),
            "logs": logs, "observations": obs}


def ready(bas):
    """guard.ready on one 1 s sample -> None when steady, else the reason. Stub: always steady."""
    if not MODE["live"]:
        return None
    from monitor import baseline
    return guard.ready(baseline.sample(1.0), bas)


def heal_check(smp, bas, tgt, until):
    """Live heal guard -> abort reason or None. Hard deadline (monotonic `until`), kill switch, and collateral
    damage outside the target's blast radius (from guard.approve). Error-rate and p95 aborts are off on purpose:
    they are the symptoms being healed."""
    if time.monotonic() >= until:
        return "heal deadline reached"
    return guard.check({"t": 0, "error_rate": 0.0, "p95_ms": 0, "health": smp.get("health", {})}, bas, tgt)


def verify(bas, since, until, tgt=None):
    """Sample 1 s at a time until guard.ready passes runner.REC_OK times in a row, or `until` (monotonic).
    -> {"healed", "recovery_s" (first steady sample - since), "reason", "abort", "samples"}"""
    _need_live("verify")
    from monitor import baseline, runner
    run, fst, why, num = 0, None, "no sample taken", 0
    while time.monotonic() < until:
        t_a = time.monotonic()
        smp = baseline.sample(1.0)
        num += 1
        abr = heal_check(smp, bas, tgt, float("inf")) if tgt else ("kill switch (STOP file)" if guard.kill_switch()
                                                                     else None)
        if abr:
            return {"healed": False, "recovery_s": None, "reason": abr, "abort": abr, "samples": num}
        why = guard.ready(smp, bas)
        if why is None:
            fst = t_a if run == 0 else fst
            run += 1
            if run >= runner.REC_OK:
                rec = round(fst - since)
                log("verify", f"steady after {rec}s ({num} samples)")
                return {"healed": True, "recovery_s": rec, "reason": None, "abort": None, "samples": num}
        else:
            run, fst = 0, None
    log("verify", f"not steady: {why}")
    return {"healed": False, "recovery_s": None, "reason": why, "abort": None, "samples": num}
