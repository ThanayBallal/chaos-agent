"""P3 helper: before/after comparison of experiment results, called by P5 after a fix is applied.
compare(rsl_before, rsl_after, fnd_before, fnd_after) -> dict matching contracts.md comparison.json."""

ERR_DROP = 0.10  # error_rate must drop by at least this much (absolute)
REC_DROP = 0.30  # recovery_s must drop by at least this fraction


def improved(bef, aft):
    """contracts.md: data_survived went false->true, OR error_rate dropped >= 0.10, OR recovery_s dropped >= 30%.
    A criterion with a missing (None) value on either side does not count."""
    if bef.get("data_survived") is False and aft.get("data_survived") is True:
        return True
    eb, ea = bef.get("error_rate"), aft.get("error_rate")
    if eb is not None and ea is not None and round(eb - ea, 6) >= ERR_DROP:
        return True
    rb, ra = bef.get("recovery_s"), aft.get("recovery_s")
    return bool(rb is not None and ra is not None and rb > 0 and (rb - ra) / rb >= REC_DROP)


def _pairs(rsl_before, rsl_after):
    """Match each before-result to its after-result: same id, else same target+fault."""
    by_id = {res["id"]: res for res in rsl_after}
    by_tf = {(res["target"], res["fault"]): res for res in rsl_after}
    for bef in rsl_before:
        aft = by_id.get(bef["id"])
        if aft is None or (aft["target"], aft["fault"]) != (bef["target"], bef["fault"]):
            aft = by_tf.get((bef["target"], bef["fault"]))
        if aft is not None:
            yield bef, aft


def _patch_ids(fnd_after, pat):
    if pat:
        return [itm["id"] for itm in pat.get("patches", [])]
    return list((fnd_after or {}).get("patch_ids", []))


def compare(rsl_before, rsl_after, fnd_before, fnd_after, pat=None):
    """Return comparison.json. Experiments present in both runs are compared; each pair is [before, after].
    pat (optional patch.json) supplies patch_ids; without it they are taken from fnd_after["patch_ids"] if present."""
    exps = []
    for bef, aft in _pairs(rsl_before, rsl_after):
        exps.append({"id": bef["id"], "target": bef["target"], "fault": bef["fault"],
                     "improved": improved(bef, aft),
                     **{key: [bef.get(key), aft.get(key)]
                        for key in ("error_rate", "p95_ms", "recovery_s", "data_survived")}})
    return {"patch_ids": _patch_ids(fnd_after, pat),
            "score": {"before": (fnd_before or {}).get("resilience_score"),
                      "after": (fnd_after or {}).get("resilience_score")},
            "experiments": exps}
