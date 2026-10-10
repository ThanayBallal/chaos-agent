"""P4: findings -> data/patch.json (contracts.md "patch.json"). P1 applies the fixes live; Robot Shop files are never edited.

{"patches": [{"id": "p1", "experiment": "e1", "service": "redis", "fix": "redis_aof",
              "reason": "cart/server.js:379 carts stored only in redis, no persistence",
              "expected": "carts survive a redis crash"}],
 "code_suggestions": [{"file": "cart/server.js", "line": 399, "suggestion": "set redisConnected=false on error"}]}

Rules decide which patches exist:
- redis data/state loss (data_survived false, a "data loss" finding, or a state-loss code weakness on redis) -> redis_aof on redis
- a killed dependency with slow (> SLOW_S) or no recovery -> restart_policy on that service
data_survived and recovery_s come from the results (rsl) when given, else from the findings' evidence text.
At most ONE patch per service per round, priority in FIXES order (redis_aof over restart_policy). exclude lists
fixes already tried in earlier rounds, so the next round picks the next fix ("at most 2 fix attempts" = 2 rounds).
Everything else (stale health flags, missing timeouts, publish-before-commit, ...) -> code_suggestions citing a
code-review file:line. The LLM, when available, only words reason/expected/suggestions; anything that is not an
eligible patch (fix not in FIXES, service not in depmap, rule not met) or not a cited file:line is dropped.
Run standalone: python -m agent.patcher [--mock]
"""
import json
import re
from pathlib import Path

from agent import code_review, llm

ROT = Path(__file__).resolve().parent.parent
OUT = ROT / "data" / "patch.json"
FIXES = ("redis_aof", "restart_policy")  # also the priority order
SLOW_S = 10  # recovery longer than this after a kill counts as slow
LOSS = re.compile(r"data[- ]loss|lost data|data (was|is|were) lost|data_survived false", re.I)
STATE = re.compile(r"state|persisten|no volume|only copy", re.I)
REC = re.compile(r"recovery (\d+(?:\.\d+)?)s")

SYS = f"""You word remediation for chaos-engineering findings on the Docker Compose app "robot-shop".
You get the eligible live patches (already decided: fix, service, experiment) and the findings with the
code-review weaknesses behind them (file:line).
Return:
- patches: for each eligible patch, the same experiment/service/fix plus
  reason: why, citing the code weakness file:line and the measured evidence; expected: what the re-run should show.
  fix must be one of {list(FIXES)}. Do not add patches that are not in the eligible list.
- code_suggestions: application-code changes (stale health flags, missing timeouts, retries with backoff,
  side effects done before a call that can still fail). file and line must be copied from code_weaknesses.
Never edit robot-shop files. Only use numbers that appear in the data."""


def _risks(exp):
    """Code-review weaknesses behind an experiment: this run's, else from data/code_review.json by target+fault."""
    rsk = code_review.risks_for(exp.get("id"))
    if rsk:
        return rsk
    try:
        rev = json.loads(code_review.OUT.read_text())
    except (OSError, ValueError):
        return []
    return [itm for itm in rev.get("risks", [])
            if itm.get("dependency") == exp.get("target") and itm.get("fault") == exp.get("fault")]


def _context(fnd, pln):
    pex = {exp["id"]: exp for exp in pln or []}
    out = []
    for itm in fnd.get("findings", []):
        exp = pex.get(itm["experiment"], {"id": itm["experiment"], "target": itm["target"]})
        out.append((itm, exp, _risks(exp)))
    return out


def _redis(dep):
    nam = [svc["name"] for svc in dep["services"]]
    return "redis" if "redis" in nam else next((itm for itm in nam if "redis" in itm), None)


def measured(itm, res):
    """(data_lost, recovered, recovery seconds) for one finding: from its result when given, else its evidence text.
    data_lost is True / False / None (not probed)."""
    if res is not None:
        sec, srv = res.get("recovery_s"), res.get("data_survived")
        return None if srv is None else not srv, sec is not None, sec
    evi = itm.get("evidence", "")
    hit = REC.search(evi)
    lost = True if LOSS.search(f"{itm.get('weakness', '')} {evi}") else None
    return lost, "no recovery" not in evi, float(hit.group(1)) if hit and "no recovery" not in evi else None


def eligible(fnd, pln, dep, rsl=None):
    """Rule-based patches {(fix, service, experiment): template patch}, plus the cited (file, line) pairs."""
    rby = {res["id"]: res for res in rsl} if rsl is not None else {}
    red, nam = _redis(dep), {svc["name"] for svc in dep["services"]}
    out, cit = {}, {}
    for itm, exp, rsk in _context(fnd, pln):
        for rkx in rsk:
            cit[(rkx["file"], rkx["line"])] = rkx
        tgt, eid = itm["target"], itm["experiment"]
        lost, ok, sec = measured(itm, rby.get(eid) if rsl is not None else None)
        sta = [rkx for rkx in rsk if rkx.get("dependency") == red and STATE.search(rkx.get("weakness", ""))]
        if red and lost is not False and (sta or (tgt == red and lost)):  # a measured survival wins
            why = f"{sta[0]['file']}:{sta[0]['line']} {sta[0]['weakness']}" if sta else itm["weakness"]
            out.setdefault(("redis_aof", red, eid), {
                "experiment": eid, "service": red, "fix": "redis_aof",
                "reason": f"{why}; measured: {itm['evidence']}",
                "expected": f"data written to {red} survives a {red} crash (data_survived true on the re-run)"})
        if exp.get("fault") == "kill" and tgt in nam and (not ok or (sec is not None and sec > SLOW_S)):
            out.setdefault(("restart_policy", tgt, eid), {
                "experiment": eid, "service": tgt, "fix": "restart_policy",
                "reason": f"{tgt} was killed and {'did not recover' if not ok else f'took {sec:g}s to recover'}; "
                          f"measured: {itm['evidence']}",
                "expected": f"{tgt} restarts on its own after a kill"
                            + (f" and recovers faster than {sec:g}s" if ok else " instead of staying down")})
    return out, cit


def _tried(key, exclude):
    """exclude entries: a fix name ("redis_aof"), "service:fix", or a patch dict from an earlier round."""
    fix, svc, _ = key
    for itm in exclude or ():
        if isinstance(itm, dict):
            itm = f"{itm.get('service')}:{itm.get('fix')}"
        if itm in (fix, f"{svc}:{fix}"):
            return True
    return False


def pick(elg, exclude=()):
    """One patch per service: the highest-priority fix not tried yet (first experiment wins on ties)."""
    out, see = {}, set()
    for key in sorted(elg, key=lambda key: FIXES.index(key[0])):
        if key[1] not in see and not _tried(key, exclude):
            see.add(key[1])
            out[key] = elg[key]
    return out


def suggestions(cit, pat):
    """Template code suggestions: every cited weakness except state loss already covered by a redis_aof patch."""
    aof = any(fix == "redis_aof" for fix, _, _ in pat)
    return [{"file": fil, "line": lin, "suggestion": rkx["fix"]} for (fil, lin), rkx in cit.items()
            if rkx.get("fix") and not (aof and STATE.search(rkx.get("weakness", "")))]


def schema(dep):
    nam = sorted(svc["name"] for svc in dep["services"])
    txt, num = {"type": "string"}, {"type": "integer"}
    pat = {"type": "object", "additionalProperties": False,
           "required": ["experiment", "service", "fix", "reason", "expected"],
           "properties": {"experiment": txt, "service": {"type": "string", "enum": nam},
                          "fix": {"type": "string", "enum": list(FIXES)}, "reason": txt, "expected": txt}}
    sug = {"type": "object", "additionalProperties": False, "required": ["file", "line", "suggestion"],
           "properties": {"file": txt, "line": num, "suggestion": txt}}
    return {"type": "object", "additionalProperties": False, "required": ["patches", "code_suggestions"],
            "properties": {"patches": {"type": "array", "items": pat},
                           "code_suggestions": {"type": "array", "items": sug}}}


def clean(raw, elg, cit, dep):
    """Keep only valid LLM items: fix in FIXES, service in depmap, an eligible patch, cited file:line. Drop the rest."""
    if not isinstance(raw, dict):
        raise ValueError("reply must be an object with 'patches' and 'code_suggestions'")
    nam = {svc["name"] for svc in dep["services"]}
    pat, sug, drp = {}, [], []
    for itm in raw.get("patches") or []:
        key = (itm.get("fix"), itm.get("service"), itm.get("experiment")) if isinstance(itm, dict) else None
        txt = isinstance(itm, dict) and all(isinstance(itm.get(fld), str) and itm[fld].strip()
                                            for fld in ("reason", "expected"))
        if key and key[0] in FIXES and key[1] in nam and key in elg and txt and key not in pat:
            pat[key] = {"experiment": key[2], "service": key[1], "fix": key[0],
                        "reason": itm["reason"].strip(), "expected": itm["expected"].strip()}
        else:
            drp.append(f"patch {key}")
    for itm in raw.get("code_suggestions") or []:
        key = (itm.get("file"), itm.get("line")) if isinstance(itm, dict) else None
        if key in cit and isinstance(itm.get("suggestion"), str) and itm["suggestion"].strip():
            sug.append({"file": key[0], "line": key[1], "suggestion": itm["suggestion"].strip()})
        else:
            drp.append(f"suggestion {key}")
    if drp:
        print(f"[patcher] dropped {len(drp)} invalid LLM items: {', '.join(map(str, drp[:4]))}")
    return pat, sug


def prompt(fnd, pln, elg):
    dat = {"eligible_patches": [{"fix": fix, "service": svc, "experiment": eid} for fix, svc, eid in elg],
           "findings": [{"experiment": exp.get("id"), "fault": exp.get("fault"), "target": itm["target"],
                         "severity": itm["severity"], "weakness": itm["weakness"], "evidence": itm["evidence"],
                         "code_weaknesses": [{key: rkx.get(key) for key in ("file", "line", "weakness", "fix")}
                                             for rkx in rsk]}
                        for itm, exp, rsk in _context(fnd, pln)]}
    return f"Return {{\"patches\": [...], \"code_suggestions\": [...]}}.\n\n{json.dumps(dat, indent=1)}"


def make(fnd, pln, dep, rsl=None, exclude=(), save=True):
    """Return patch.json for these findings and write it to data/patch.json.
    rsl: the result.json list behind fnd; when given, data_survived and recovery_s are read from it directly.
    exclude: fixes already tried (fix name, "service:fix" or earlier patch dicts); at most one patch per service."""
    alg, cit = eligible(fnd, pln, dep, rsl)
    elg = pick(alg, exclude)
    pat, sug, src = {}, [], "template"
    if fnd.get("findings") and (elg or cit):
        try:
            pat, sug = llm.ask_json(SYS, prompt(fnd, pln, elg), schema=schema(dep),
                                    check=lambda raw: clean(raw, elg, cit, dep))
            src = "llm"
        except Exception as err:
            print(f"[patcher] LLM wording unavailable ({str(err)[:120]}); using templates")
    pts = [pat.get(key, tpl) for key, tpl in elg.items()]  # every eligible patch, LLM wording when valid
    see = {(itm["file"], itm["line"]) for itm in sug}
    sug += [itm for itm in suggestions(cit, alg) if (itm["file"], itm["line"]) not in see]
    pch = {"patches": [{"id": f"p{idx}", **itm} for idx, itm in enumerate(pts, 1)], "code_suggestions": sug}
    print(f"[patcher] {len(pch['patches'])} live patches ({', '.join(itm['fix'] + ' ' + itm['service'] for itm in pts) or 'none'}), "
          f"{len(sug)} code suggestions ({src})")
    if save:
        try:
            OUT.parent.mkdir(exist_ok=True)
            OUT.write_text(json.dumps(pch, indent=2))
        except OSError as err:
            print(f"[patcher] could not write {OUT}: {err}")
    return pch


def _cli():
    """python -m agent.patcher [--mock]: patch the last run's data/findings.json + data/plan.json.
    --mock rebuilds the results like main.py --mock does (mock_results.json matched by target)."""
    import sys
    dat = ROT / "data"
    rd = lambda nam: json.loads((dat / nam).read_text())
    pln = rd("plan.json")
    if "--mock" in sys.argv:
        dep, mck = rd("mock_depmap.json"), {res["target"]: res for res in rd("mock_results.json")}
        rsl = [{**mck[exp["target"]], "id": exp["id"], "fault": exp["fault"]} for exp in pln if exp["target"] in mck]
    else:
        dep, rsl = rd("depmap.json" if (dat / "depmap.json").exists() else "mock_depmap.json"), None
    make(rd("findings.json"), pln, dep, rsl)
    print(f"[patcher] wrote {OUT}")


if __name__ == "__main__":
    _cli()
