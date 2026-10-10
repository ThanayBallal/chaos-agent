"""P4: rank experiments. Deterministic fallback works now; TODO(P4): LLM plan via llm.ask_json."""
from safety.allowlist import FAULTS

DEF = {"db": "pause", "cache": "kill", "queue": "kill", "proxy": "latency", "app": "kill"}
PRM = {"kill": {"duration_s": 30}, "pause": {"duration_s": 30},
       "latency": {"duration_s": 30, "delay_ms": 500}, "cpu_stress": {"duration_s": 30, "cpus": 1}}


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


def plan(dep, bas, top=3):
    """Return a plan.json-shaped list. TODO(P4): LLM first, validate, else fallback."""
    pln = fallback(dep, top)
    assert all(exp["fault"] in FAULTS for exp in pln)
    return pln
