"""The calls the heal nodes make into team modules, with their exact signatures. STUBS for this step.

Each stub logs and returns contract-shaped fake data marked "stub": True. To wire a real module in, replace
the stub with an import of the function named in REAL; the signature already matches.

  review       -> agent.code_review.review(dep)                                (exists)
  heal_fix     -> heal one injected fault now; action from graph.HEAL_CATALOG  (owner/module not decided yet)
  make         -> agent.patcher.make(fnd, pln, dep, rsl=rsl, exclude=exclude)  (P4: rsl and exclude not added yet)
  apply        -> setup.patch.apply(pat) -> None, live fixes, not compose files (P1: not written yet)
  revert_patch -> setup.patch.revert() -> None, undoes round-1 patches only    (P1: not written yet)
  compare      -> monitor.compare.compare(rsl_before, rsl_after, fnd_before, fnd_after)  (P3: not written yet)
  revert_all   -> safety.guard.revert_all(): injected faults only, never patches (exists)
"""

REAL = {"review": "agent.code_review.review", "heal_fix": None, "make": "agent.patcher.make",
        "apply": "setup.patch.apply", "revert_patch": "setup.patch.revert", "compare": "monitor.compare.compare",
        "revert_all": "safety.guard.revert_all"}
FIXES = {"redis": ("redis_aof", "restart_policy")}  # stub preference order per service; others get restart_policy


def log(nam, msg):
    print(f"[heal.ports] {nam}: {msg} (stub)")


def review(dep):
    """agent.code_review.review(dep) -> {"source", "files_read", "services", "risks": [...]}"""
    nam = {svc["name"] for svc in dep.get("services", [])}
    rsk = [{"service": "cart", "dependency": "redis", "file": "cart/server.js", "line": 0, "quote": "STUB",
            "weakness": "STUB: cart keeps its only copy of user state in redis", "fault": "kill",
            "params": {"duration_s": 30}, "hypothesis": "carts are lost after redis restarts",
            "fix": "give redis a volume with AOF persistence", "weight": 3}] if "redis" in nam else []
    log("review", f"{len(rsk)} risks")
    return {"source": "stub", "files_read": 0, "services": sorted(nam), "risks": rsk, "stub": True}


def heal_fix(action, target):
    """Undo the injected fault on target now (start_service, unpause_service, ..., restart_web) -> None.
    TODO: real call not assigned yet (docker start/unpause, tc qdisc del, kill the cpu loops, restart web)."""
    log("heal_fix", f"{action}({target})")


def make(fnd, pln, dep, rsl=None, exclude=None):
    """agent.patcher.make(fnd, pln, dep, rsl=rsl, exclude=exclude) -> patch.json {"patches", "code_suggestions"}.
    rsl = results the findings came from; exclude = patch dicts already tried (round 1), never proposed again.
    TODO: real call agent.patcher.make(fnd, pln, dep, rsl=rsl, exclude=exclude) once P4 adds rsl and exclude."""
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
    return {"patches": pts, "code_suggestions": [], "stub": True}


def apply(pat):
    """setup.patch.apply(pat) -> None. Applies the patches live.
    TODO: real call setup.patch.apply(pat) once P1 writes setup/patch.py."""
    log("apply", f"{', '.join(itm['fix'] + ' ' + itm['service'] for itm in pat.get('patches', [])) or 'nothing'}")


def revert_patch():
    """setup.patch.revert() -> None. Undoes the applied patches; only used between hardening round 1 and 2.
    TODO: real call setup.patch.revert() once P1 writes setup/patch.py."""
    log("revert_patch", "round-1 patches undone")


def compare(rsl_before, rsl_after, fnd_before, fnd_after):
    """monitor.compare.compare(...) -> comparison.json {"patch_ids", "score", "experiments": [...]}.
    Stub "improved" is simply error_rate going down; the contract rule lives in monitor/compare.py.
    TODO: real call monitor.compare.compare(rsl_before, rsl_after, fnd_before, fnd_after) once P3 writes it."""
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
    """safety.guard.revert_all() -> None. Runs every registered fault revert; patches are not registered there."""
    log("revert_all", "no registered faults")
