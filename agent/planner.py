"""P4: rank experiments. LLM plan via llm.ask_json, validated against the safety allowlist; deterministic fallback."""
import json

from agent import llm
from safety import guard
from safety.allowlist import FAULTS, MAX_DUR, P95_MAX

DEF = {"db": "pause", "cache": "kill", "queue": "kill", "proxy": "latency", "app": "kill"}
PRM = {"kill": {"duration_s": 30}, "pause": {"duration_s": 30},
       "latency": {"duration_s": 30, "delay_ms": 500}, "cpu_stress": {"duration_s": 30, "cpus": 1}}
RSK = ["high", "medium", "low"]

SYS = f"""You plan fault-injection experiments for a chaos-engineering agent testing the Docker Compose app "robot-shop".
Pick the experiments that reveal the most about the app's resilience, most informative first.

Rules:
- Use only the data you are given: dependency map, baseline and centrality (= how many services transitively depend on a service). Never invent services, metrics or measurements.
- Allowed faults and their exact params: {json.dumps({fal: sorted(prm) for fal, prm in FAULTS.items()})}. Give only the params listed for the chosen fault, all as integers.
- duration_s must be 1..{MAX_DUR}. latency delay_ms must be below {P95_MAX} (the abort threshold). cpu_stress cpus >= 1.
- Only target services that appear in the dependency map and are healthy in the baseline. Do not repeat the same target+fault.
- Prefer high-centrality shared dependencies and faults that fit the service kind (e.g. pause or latency on databases, kill on caches and queues).
- reason: why this experiment matters, citing the dependency map (name the dependents) and the centrality score.
- hypothesis: a falsifiable prediction of what will be observed during the fault and after it is reverted. Do not put numbers you were not given in it.
- risk: high | medium | low. rank: 1 = run first."""


def centrality(dep):
    """Number of services that transitively depend on each service."""
    rev = {svc["name"]: set() for svc in dep["services"]}
    for edg in dep["edges"]:
        rev.setdefault(edg["dst"], set()).add(edg["src"])
    out = {}
    for nam in rev:
        see, stk = set(), [nam]
        while stk:
            for src in rev.get(stk.pop(), ()):
                if src not in see:
                    see.add(src)
                    stk.append(src)
        out[nam] = len(see)
    return out


def fallback(dep, top=3):
    cen = centrality(dep)
    knd = {svc["name"]: svc["kind"] for svc in dep["services"]}
    rnk = sorted(cen, key=lambda nam: -cen[nam])[:top]
    pln = []
    for idx, nam in enumerate(rnk, 1):
        fal = DEF[knd[nam]]
        pln.append({"id": f"e{idx}", "target": nam, "fault": fal, "params": dict(PRM[fal]),
                    "risk": "high" if cen[nam] >= 3 else "medium", "rank": idx,
                    "reason": f"{cen[nam]} services depend on {nam} (transitively)",
                    "hypothesis": f"dependents of {nam} degrade while it is down"})
    return pln


def schema(dep):
    """Structured-output schema for the LLM reply: {"experiments": [plan item without id]}."""
    num = {"type": "integer"}
    txt = {"type": "string"}
    itm = {"type": "object", "additionalProperties": False,
           "required": ["target", "fault", "params", "risk", "rank", "reason", "hypothesis"],
           "properties": {
               "target": {"type": "string", "enum": sorted(svc["name"] for svc in dep["services"])},
               "fault": {"type": "string", "enum": sorted(FAULTS)},
               "params": {"type": "object", "additionalProperties": False, "required": ["duration_s"],
                          "properties": {key: num for key in sorted(set().union(*FAULTS.values()))}},
               "risk": {"type": "string", "enum": RSK},
               "rank": num, "reason": txt, "hypothesis": txt}}
    return {"type": "object", "additionalProperties": False, "required": ["experiments"],
            "properties": {"experiments": {"type": "array", "items": itm}}}


def validate(raw, dep, bas, top=3):
    """Normalize an LLM reply into plan.json items; raise ValueError naming the first problem."""
    exs = raw.get("experiments") if isinstance(raw, dict) else raw
    if not isinstance(exs, list) or not exs:
        raise ValueError("'experiments' must be a non-empty list")
    if all(isinstance(exp, dict) and isinstance(exp.get("rank"), int) for exp in exs):
        exs = sorted(exs, key=lambda exp: exp["rank"])
    hea = bas.get("health", {})
    pln, see = [], set()
    for idx, exp in enumerate(exs[:top], 1):
        if not isinstance(exp, dict):
            raise ValueError(f"experiment {idx} is not an object")
        tgt, fal = exp.get("target"), exp.get("fault")
        tag = f"experiment {idx} ({tgt}/{fal})"
        if fal not in FAULTS:
            raise ValueError(f"{tag}: fault must be one of {sorted(FAULTS)}")
        src = exp.get("params") or {}
        prm = {key: src.get(key) for key in sorted(FAULTS[fal])}
        bad = [key for key, val in prm.items() if not isinstance(val, int) or isinstance(val, bool) or val < 1]
        if bad:
            raise ValueError(f"{tag}: params {bad} must be positive integers")
        if fal == "latency" and prm["delay_ms"] >= P95_MAX:
            raise ValueError(f"{tag}: delay_ms must be below {P95_MAX}")
        if hea.get(tgt, "healthy") != "healthy":
            raise ValueError(f"{tag}: target is '{hea[tgt]}' in the baseline")
        if (tgt, fal) in see:
            raise ValueError(f"{tag}: duplicate target+fault")
        see.add((tgt, fal))
        for key in ("reason", "hypothesis"):
            if not isinstance(exp.get(key), str) or not exp[key].strip():
                raise ValueError(f"{tag}: '{key}' must be a non-empty string")
        itm = {"id": f"e{idx}", "target": tgt, "fault": fal, "params": prm,
               "risk": exp.get("risk") if exp.get("risk") in RSK else "medium", "rank": idx,
               "reason": exp["reason"].strip(), "hypothesis": exp["hypothesis"].strip()}
        rej = guard.approve(itm, dep)
        if rej:
            raise ValueError(f"{tag}: rejected by safety guard: {rej}")
        pln.append(itm)
    return pln


def prompt(dep, bas, top):
    cen = centrality(dep)
    pub = lambda obj: {key: val for key, val in obj.items() if not key.startswith("_")}
    dat = {"max_experiments": top, "centrality": dict(sorted(cen.items(), key=lambda kv: -kv[1])),
           "services": [{key: svc.get(key) for key in ("name", "kind", "depends_on")} for svc in dep["services"]],
           "edges": dep["edges"], "baseline": pub(bas)}
    return f"Plan at most {top} experiments. Return {{\"experiments\": [...]}}.\n\n{json.dumps(dat, indent=1)}"


def plan(dep, bas, top=3):
    """Return a plan.json-shaped list: LLM first, validated, else deterministic fallback."""
    try:
        pln = llm.ask_json(SYS, prompt(dep, bas, top), schema=schema(dep),
                           check=lambda raw: validate(raw, dep, bas, top))
        print(f"[planner] LLM plan with {len(pln)} experiments")
    except Exception as err:
        print(f"[planner] LLM plan unavailable ({err}); using deterministic fallback")
        pln = fallback(dep, top)
    assert all(exp["fault"] in FAULTS for exp in pln)
    return pln
