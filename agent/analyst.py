"""P4: turn measured results into ranked findings. LLM explanations + fixes, number-checked; rule-based fallback."""
import json
import re

from agent import llm

SEV = [(0.5, "critical"), (0.2, "high"), (0.05, "medium"), (0.0, "low")]
ORD = {"critical": 0, "high": 1, "medium": 2, "low": 3}
PEN = {"critical": 25, "high": 15, "medium": 7, "low": 2}
VRD = {"matched": True, "not_matched": False, "inconclusive": None}
NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
MET = ("error_rate", "p95_ms", "recovery_s")

SYS = """You analyze chaos-engineering experiments run against the Docker Compose app "robot-shop".
For each experiment you get the plan (target, fault, params, hypothesis), the MEASURED result (error_rate, p95_ms, recovery_s, aborted, abort_reason, observations), the pre-fault baseline and the dependency map.

For every experiment return:
- weakness: one or two sentences explaining what the measurements show broke and why, using the dependency map to name the affected services.
- hypothesis: "matched" if the observations and metrics confirm the hypothesis, "not_matched" if they contradict it, "inconclusive" if the data cannot decide or there is no hypothesis.
- severity: critical | high | medium | low, judged by user impact in the measured data.
- fix: concrete, actionable changes naming the service and file to change, chosen from: compose restart policy (restart: always / unless-stopped), a compose healthcheck plus depends_on condition: service_healthy, client retries with exponential backoff, connection/request timeouts, replicas or a graceful fallback. Each fix must cite the measured evidence that motivates it (quote at least one measured error_rate, p95_ms or recovery_s value).

Hard rule: every number you write must be copied verbatim from the provided data. Never invent metrics, thresholds, counts, durations or percentages. If a config value would need a number that is not in the data, describe it without a number."""


def severity(res, bas):
    dlt = res["error_rate"] - bas["error_rate"]
    if res["aborted"]:
        return "critical"
    return next(lbl for thr, lbl in SEV if dlt >= thr)


def evidence(res, bas):
    rec = f"recovery {res['recovery_s']}s" if res.get("recovery_s") is not None else "no recovery measured"
    out = (f"error_rate {bas['error_rate']} -> {res['error_rate']}, "
           f"p95 {bas['p95_ms']} -> {res['p95_ms']}ms, {rec}")
    return f"{out}, aborted: {res['abort_reason']}" if res.get("aborted") else out


def finish(fnd, rsl):
    """Sort by severity then error_rate, assign ranks, compute resilience_score."""
    err = {res["id"]: res["error_rate"] for res in rsl}
    fnd.sort(key=lambda itm: (ORD[itm["severity"]], -err[itm["experiment"]]))
    for idx, itm in enumerate(fnd, 1):
        itm["rank"] = idx
    scr = max(0, 100 - sum(PEN[itm["severity"]] for itm in fnd))
    return {"resilience_score": scr, "findings": [{"rank": itm.pop("rank"), **itm} for itm in fnd]}


def fallback(dep, bas, rsl, pln=None):
    """Rule-based findings from the measured numbers only."""
    fnd = []
    for res in rsl:
        fnd.append({"severity": severity(res, bas), "target": res["target"], "experiment": res["id"],
                    "weakness": f"{res['fault']} on {res['target']} raised errors to {res['error_rate']:.0%}",
                    "evidence": evidence(res, bas),
                    "hypothesis_matched": None,
                    "fix": f"add restart policy and healthcheck for {res['target']}; "
                           f"retries with backoff and timeouts in its dependents"})
    return finish(fnd, rsl)


def _norm(val):
    return f"{float(val):g}"


def allowed(res, bas, exp):
    """Every number present in this experiment's data, plus percent forms of fractions."""
    src = json.dumps([res, bas, exp or {}])
    out = {_norm(tok) for tok in re.findall(r"\d+(?:\.\d+)?", src)}
    for tok in list(out):
        if float(tok) <= 1:
            out |= {_norm(round(float(tok) * 100, 1)), _norm(round(float(tok) * 100))}
    return out


def measured(res):
    """Numbers that count as citing this result's measured evidence."""
    out = set()
    for key in MET:
        if isinstance(res.get(key), (int, float)):
            out.add(_norm(res[key]))
            if res[key] <= 1:
                out |= {_norm(round(res[key] * 100, 1)), _norm(round(res[key] * 100))}
    return out


def numbers(txt):
    return {_norm(tok) for tok in NUM.findall(re.sub(r"\b[1-5]xx\b", "", txt))}


def schema(rsl):
    txt = {"type": "string"}
    itm = {"type": "object", "additionalProperties": False,
           "required": ["experiment", "weakness", "hypothesis", "severity", "fix"],
           "properties": {"experiment": {"type": "string", "enum": [res["id"] for res in rsl]},
                          "weakness": txt, "fix": txt,
                          "hypothesis": {"type": "string", "enum": sorted(VRD)},
                          "severity": {"type": "string", "enum": sorted(ORD, key=ORD.get)}}}
    return {"type": "object", "additionalProperties": False, "required": ["findings"],
            "properties": {"findings": {"type": "array", "items": itm}}}


def validate(raw, bas, rsl, pex):
    """Map experiment id -> checked LLM item; raise ValueError naming the first problem."""
    itms = raw.get("findings") if isinstance(raw, dict) else None
    if not isinstance(itms, list):
        raise ValueError("'findings' must be a list")
    res_by = {res["id"]: res for res in rsl}
    got = {}
    for itm in itms:
        eid = itm.get("experiment") if isinstance(itm, dict) else None
        if eid not in res_by or eid in got:
            raise ValueError(f"unknown or duplicate experiment {eid!r}")
        for key in ("weakness", "fix"):
            if not isinstance(itm.get(key), str) or not itm[key].strip():
                raise ValueError(f"{eid}: '{key}' must be a non-empty string")
        if itm.get("severity") not in ORD or itm.get("hypothesis") not in VRD:
            raise ValueError(f"{eid}: bad severity or hypothesis value")
        ok = allowed(res_by[eid], bas, pex.get(eid))
        bad = sorted((numbers(itm["weakness"]) | numbers(itm["fix"])) - ok, key=float)
        if bad:
            raise ValueError(f"{eid}: numbers {bad} are not in the measured data; remove or replace them")
        if not numbers(itm["fix"]) & measured(res_by[eid]):
            raise ValueError(f"{eid}: fix must cite a measured error_rate, p95_ms or recovery_s value")
        got[eid] = itm
    if set(got) != set(res_by):
        raise ValueError(f"missing findings for {sorted(set(res_by) - set(got))}")
    return got


def prompt(dep, bas, rsl, pex):
    pub = lambda obj: {key: val for key, val in obj.items() if not key.startswith("_")}
    dat = {"baseline": pub(bas),
           "dependency_map": {"services": [{key: svc.get(key) for key in ("name", "kind", "depends_on")}
                                           for svc in dep["services"]], "edges": dep["edges"]},
           "experiments": [{"plan": pex.get(res["id"]), "result": res} for res in rsl]}
    return f"Analyze every experiment. Return {{\"findings\": [...]}}.\n\n{json.dumps(dat, indent=1)}"


def analyze(dep, bas, rsl, pln=None):
    """Return a dict matching contracts.md findings.json. LLM first, else rule-based fallback."""
    if not rsl:
        return fallback(dep, bas, rsl, pln)
    pex = {exp["id"]: exp for exp in pln or []}
    try:
        got = llm.ask_json(SYS, prompt(dep, bas, rsl, pex), schema=schema(rsl),
                           check=lambda raw: validate(raw, bas, rsl, pex))
    except Exception as err:
        print(f"[analyst] LLM analysis unavailable ({err}); using rule-based fallback")
        return fallback(dep, bas, rsl, pln)
    print(f"[analyst] LLM analysis for {len(got)} experiments")
    fnd = []
    for res in rsl:
        itm = got[res["id"]]
        flr = severity(res, bas)
        hyp = pex.get(res["id"], {}).get("hypothesis")
        fnd.append({"severity": min(flr, itm["severity"], key=ORD.get), "target": res["target"],
                    "experiment": res["id"], "weakness": itm["weakness"].strip(),
                    "evidence": evidence(res, bas),
                    "hypothesis_matched": VRD[itm["hypothesis"]] if hyp else None,
                    "fix": itm["fix"].strip()})
    return finish(fnd, rsl)
