"""The calls the heal nodes make into team modules, with their exact signatures. STUBS for this step.

Each stub logs and returns contract-shaped fake data marked "stub": True. To wire a real module in, replace
the stub with an import of the function named in REAL; the signature already matches.

  review     -> agent.code_review.review(dep)                                  (exists)
  make       -> agent.patcher.make(fnd, pln, dep, rsl=rsl)                     (P4: rsl = before-results, not yet added)
  apply      -> setup.patch.apply(pat) -> None, live fixes, not compose files  (P1: not written yet)
  compare    -> monitor.compare.compare(rsl_before, rsl_after, fnd_before, fnd_after)  (P3: not written yet)
  revert_all -> safety.guard.revert_all(): injected faults only, never patches (exists)
"""

REAL = {"review": "agent.code_review.review", "make": "agent.patcher.make", "apply": "setup.patch.apply",
        "compare": "monitor.compare.compare", "revert_all": "safety.guard.revert_all"}


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


def make(fnd, pln, dep, rsl=None):
    """agent.patcher.make(fnd, pln, dep, rsl=rsl) -> patch.json {"patches": [...], "code_suggestions": [...]}"""
    pex = {exp["id"]: exp for exp in pln}
    pts = []
    for itm in fnd.get("findings", []):
        tgt = pex.get(itm["experiment"], {}).get("target", itm["target"])
        pts.append({"id": f"p{len(pts) + 1}", "experiment": itm["experiment"], "service": tgt,
                    "fix": "redis_aof" if tgt == "redis" else "restart_policy",
                    "reason": f"STUB: {itm['weakness']}", "expected": f"{tgt} survives the fault"})
    log("make", f"{len(pts)} patches from {len(fnd.get('findings', []))} findings, {len(rsl or [])} before-results")
    return {"patches": pts, "code_suggestions": [], "stub": True}


def apply(pat):
    """setup.patch.apply(pat) -> None. Applies the patches live."""
    log("apply", f"{', '.join(itm['fix'] + ' ' + itm['service'] for itm in pat.get('patches', [])) or 'nothing'}")


def compare(rsl_before, rsl_after, fnd_before, fnd_after):
    """monitor.compare.compare(...) -> comparison.json {"patch_ids", "score", "experiments": [...]}"""
    aft = {res["id"]: res for res in rsl_after}
    exs = [{"id": bef["id"], "target": bef["target"], "fault": bef["fault"], "improved": True,
            **{key: [bef.get(key), aft[bef["id"]].get(key)]
               for key in ("error_rate", "p95_ms", "recovery_s", "data_survived")}}
           for bef in rsl_before if bef["id"] in aft]
    cmp = {"patch_ids": [], "score": {"before": fnd_before.get("resilience_score"),
                                      "after": fnd_after.get("resilience_score")},
           "experiments": exs, "stub": True}
    log("compare", f"score {cmp['score']['before']} -> {cmp['score']['after']}")
    return cmp


def revert_all():
    """safety.guard.revert_all() -> None. Runs every registered fault revert; patches are not registered there."""
    log("revert_all", "no registered faults")
