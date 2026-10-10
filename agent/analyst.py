"""P4: turn measured results into ranked findings. Rule-based stub works now; TODO(P4): LLM explanations + fixes."""

SEV = [(0.5, "critical"), (0.2, "high"), (0.05, "medium"), (0.0, "low")]


def severity(res, bas):
    dlt = res["error_rate"] - bas["error_rate"]
    if res["aborted"]:
        return "critical"
    return next(lbl for thr, lbl in SEV if dlt >= thr)


def analyze(dep, bas, rsl, pln=None):
    """Return a dict matching contracts.md findings.json."""
    ord_ = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    fnd = []
    for res in rsl:
        fnd.append({"severity": severity(res, bas), "target": res["target"], "experiment": res["id"],
                    "weakness": f"{res['fault']} on {res['target']} raised errors to {res['error_rate']:.0%}",
                    "evidence": f"error_rate {bas['error_rate']} -> {res['error_rate']}, "
                                f"p95 {bas['p95_ms']} -> {res['p95_ms']}ms, recovery {res['recovery_s']}s",
                    "hypothesis_matched": None,
                    "fix": "TODO(P4): LLM-generated fix from evidence"})
    err = {res["id"]: res["error_rate"] for res in rsl}
    fnd.sort(key=lambda itm: (ord_[itm["severity"]], -err[itm["experiment"]]))
    for idx, itm in enumerate(fnd, 1):
        itm["rank"] = idx
    pen = {"critical": 25, "high": 15, "medium": 7, "low": 2}
    scr = max(0, 100 - sum(pen[itm["severity"]] for itm in fnd))
    return {"resilience_score": scr, "findings": fnd}
