"""Self-healing chaos loop as a LangGraph StateGraph.

understand -> chaos_agent -> guard_approve -> inject -> observe -> diagnosis_agent -> remediation_agent -> apply_fix
-> verify -> (healed) harden_agent -> apply_hardening -> rerun -> compare -> guard_revert -> report
          -> (not healed, attempts < 2) observe ...   -> (otherwise) guard_revert -> report
guard_approve -> (rejected) report.

Every node is a STUB: it logs and returns plausible fake data marked "stub": True and never writes data/*.json.
Nodes that will call team modules already call them through heal/ports.py with the exact target signatures:
understand -> code_review.review, harden_agent -> patcher.make(..., rsl=rsl), apply_hardening -> setup.patch.apply,
compare -> monitor.compare.compare, guard_revert -> guard.revert_all (injected faults only, never patches).
The stub scenario comes from config["configurable"]["scenario"]: healed | retry (default) | unhealed | rejected.
"""
import json
from pathlib import Path
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from heal import ports
from safety.allowlist import FAULTS

DAT = Path(__file__).resolve().parent.parent / "data"
MAX_ATTEMPTS = 2  # contracts.md: at most 2 fix attempts per run
SCENARIOS = ("retry", "healed", "unhealed", "rejected")
DFL_EXP = {"id": "e1", "target": "redis", "fault": "kill", "params": {"duration_s": 30}, "risk": "high", "rank": 1,
           "reason": "STUB: default experiment (no code-review risk)",
           "hypothesis": "cart requests fail while redis is down; recovers after restart"}


class HealState(TypedDict, total=False):
    prompt: str
    app_model: dict
    depmap: dict
    baseline: dict
    experiment: dict
    symptoms: dict
    diagnosis: dict
    fix_plan: dict
    applied_fixes: list
    verify: dict
    attempts: int
    hardening: dict
    rerun: dict
    report: dict


def log(nam, msg):
    print(f"[heal] {nam}: {msg}")


def _scenario(config):
    return ((config or {}).get("configurable") or {}).get("scenario", "retry")


def _load(nam):
    return json.loads((DAT / nam).read_text())


# ---- routing (pure functions of the state) ----

def route_approval(state):
    """'rejected' if the guard gave a rejection reason, else 'approved'."""
    return "rejected" if (state.get("experiment") or {}).get("rejection") else "approved"


def route_verify(state):
    """'healed' if verify says so; else 'retry' while attempts < MAX_ATTEMPTS; else 'give_up'."""
    if (state.get("verify") or {}).get("healed"):
        return "healed"
    return "retry" if state.get("attempts", 0) < MAX_ATTEMPTS else "give_up"


# ---- nodes ----

def understand(state, config: RunnableConfig):
    """Load the (mock) depmap and baseline, then review the app's source: code_review.review(dep)."""
    dep, bas = _load("mock_depmap.json"), _load("mock_baseline.json")
    try:
        mdl = ports.review(dep)
    except Exception as err:
        mdl = {"source": "unavailable", "risks": [], "error": str(err)[:200]}
    log("understand", f"code review source={mdl.get('source')} files={mdl.get('files_read', 0)} "
                      f"risks={len(mdl.get('risks', []))}")
    return {"depmap": dep, "baseline": bas, "app_model": mdl}


def chaos_agent(state, config: RunnableConfig):
    """STUB: turn the top code-review risk into one plan.json experiment."""
    nam = {svc["name"] for svc in (state.get("depmap") or {}).get("services", [])}
    rsk = next((itm for itm in (state.get("app_model") or {}).get("risks", [])
                if itm.get("dependency") in nam and itm.get("fault") in FAULTS), None)
    if rsk:
        exp = {"id": "e1", "target": rsk["dependency"], "fault": rsk["fault"], "params": dict(rsk["params"]),
               "risk": "high", "rank": 1, "reason": f"code review: {rsk['file']}:{rsk['line']} {rsk['weakness']}"[:600],
               "hypothesis": rsk["hypothesis"]}
    else:
        exp = dict(DFL_EXP, params=dict(DFL_EXP["params"]))
    exp["stub"] = True
    log("chaos_agent", f"{exp['fault']} {exp['target']} {exp['params']}")
    return {"experiment": exp}


def guard_approve(state, config: RunnableConfig):
    """STUB: approve unless the scenario is 'rejected'."""
    rej = "STUB: rejected by scenario" if _scenario(config) == "rejected" else None
    log("guard_approve", f"rejected: {rej}" if rej else "approved")
    return {"experiment": {**state["experiment"], "rejection": rej}}


def inject(state, config: RunnableConfig):
    """STUB: pretend the fault was injected."""
    exp = state["experiment"]
    log("inject", f"{exp['fault']} on {exp['target']} (not really)")
    return {"symptoms": {"id": exp["id"], "target": exp["target"], "fault": exp["fault"], "injected": True,
                         "stub": True}}


def observe(state, config: RunnableConfig):
    """STUB: result.json-like symptoms; worse before any fix, better after one."""
    exp, att = state["experiment"], state.get("attempts", 0)
    sym = {"id": exp["id"], "target": exp["target"], "fault": exp["fault"],
           "error_rate": 0.62 if att == 0 else 0.31, "p95_ms": 2400 if att == 0 else 900,
           "recovery_s": 14 if att == 0 else 8, "aborted": False, "abort_reason": None,
           "data_survived": False, "observations": [f"STUB: {exp['target']} dependents return 5xx"],
           "stub": True}
    log("observe", f"attempt {att}: error_rate={sym['error_rate']} p95={sym['p95_ms']}ms")
    return {"symptoms": sym}


def diagnosis_agent(state, config: RunnableConfig):
    """STUB: root cause plus a findings.json-shaped dict that agent.patcher consumes."""
    exp, sym = state["experiment"], state["symptoms"]
    rsk = next((itm for itm in (state.get("app_model") or {}).get("risks", [])
                if itm.get("dependency") == exp["target"] and itm.get("fault") == exp["fault"]), None)
    wek = rsk["weakness"] if rsk else f"single {exp['target']} instance with no fallback"
    fix = rsk["fix"] if rsk else "add restart: unless-stopped + healthcheck"
    fnd = {"resilience_score": 50, "findings": [{
        "rank": 1, "severity": "critical", "target": exp["target"], "experiment": exp["id"], "weakness": wek,
        "evidence": f"STUB: error_rate {state['baseline'].get('error_rate')} -> {sym['error_rate']}",
        "hypothesis_matched": True, "fix": fix}]}
    log("diagnosis_agent", wek)
    return {"diagnosis": {"root_cause": wek, "findings": fnd, "stub": True}}


def remediation_agent(state, config: RunnableConfig):
    """STUB: one patch.json-style live fix per attempt."""
    exp, att = state["experiment"], state.get("attempts", 0)
    fix = "redis_aof" if exp["target"] == "redis" and att == 0 else "restart_policy"
    pch = {"id": f"p{att + 1}", "experiment": exp["id"], "service": exp["target"], "fix": fix,
           "reason": state["diagnosis"]["root_cause"], "expected": f"{exp['target']} recovers from {exp['fault']}"}
    log("remediation_agent", f"{pch['id']} {fix} on {exp['target']}")
    return {"fix_plan": {"patches": [pch], "stub": True}}


def apply_fix(state, config: RunnableConfig):
    """STUB: record the fix as applied (nothing changes) and count the attempt (at most MAX_ATTEMPTS)."""
    att = state.get("attempts", 0) + 1
    new = [{**pch, "applied": False, "attempt": att, "stub": True} for pch in state["fix_plan"]["patches"]]
    log("apply_fix", f"attempt {att}: {', '.join(pch['fix'] for pch in new)} (not really)")
    return {"applied_fixes": list(state.get("applied_fixes") or []) + new, "attempts": att}


def verify(state, config: RunnableConfig):
    """STUB: healed per scenario - healed: 1st attempt, retry: 2nd attempt, unhealed: never."""
    att, scn = state.get("attempts", 0), _scenario(config)
    hld = scn == "healed" or (scn == "retry" and att >= MAX_ATTEMPTS)
    log("verify", f"attempt {att}: {'healed' if hld else 'not healed'}")
    return {"verify": {"healed": hld, "attempt": att, "stub": True}}


def guard_revert(state, config: RunnableConfig):
    """Undo the injected fault: guard.revert_all(). Applied patches are never reverted here."""
    ports.revert_all()
    exp = state["experiment"]
    rev = [f"fault {exp['id']} {exp['fault']} on {exp['target']}"]
    log("guard_revert", f"reverted {', '.join(rev)}")
    return {"report": {**(state.get("report") or {}), "reverted_faults": rev}}


def harden_agent(state, config: RunnableConfig):
    """Findings + before-results -> patch.json: patcher.make(fnd, pln, dep, rsl=rsl)."""
    pch = ports.make(state["diagnosis"]["findings"], [state["experiment"]], state["depmap"], rsl=[state["symptoms"]])
    log("harden_agent", f"{len(pch['patches'])} patches, {len(pch['code_suggestions'])} code suggestions")
    return {"hardening": {"patch": pch}}


def apply_hardening(state, config: RunnableConfig):
    """Apply the patches live: setup.patch.apply(pat)."""
    hrd = state["hardening"]
    ports.apply(hrd["patch"])
    ids = [pch["id"] for pch in hrd["patch"].get("patches", [])]
    log("apply_hardening", f"applied {', '.join(ids) or 'nothing'}")
    return {"hardening": {**hrd, "applied": ids}}


def rerun(state, config: RunnableConfig):
    """STUB: rerun the same experiment on the hardened app; result.json list + findings.json for compare."""
    exp = state["experiment"]
    res = {"id": exp["id"], "target": exp["target"], "fault": exp["fault"], "error_rate": 0.05, "p95_ms": 300,
           "recovery_s": 4, "aborted": False, "abort_reason": None, "data_survived": True,
           "observations": ["STUB: rerun after hardening"], "stub": True}
    log("rerun", f"{exp['fault']} {exp['target']}: error_rate={res['error_rate']}")
    return {"rerun": {"results": [res], "findings": {"resilience_score": 78, "findings": [], "stub": True}}}


def compare(state, config: RunnableConfig):
    """Before vs after: monitor.compare.compare(rsl_before, rsl_after, fnd_before, fnd_after)."""
    rrn = state["rerun"]
    cmp = ports.compare([state["symptoms"]], rrn["results"], state["diagnosis"]["findings"], rrn["findings"])
    log("compare", f"score {cmp['score']['before']} -> {cmp['score']['after']}")
    return {"rerun": {**rrn, "comparison": cmp}}


def report(state, config: RunnableConfig):
    """STUB: summarise the run."""
    exp = state.get("experiment") or {}
    if exp.get("rejection"):
        out = "rejected"
    elif (state.get("verify") or {}).get("healed"):
        out = "healed"
    else:
        out = "not_healed"
    rpt = {**(state.get("report") or {}), "prompt": state.get("prompt"), "outcome": out,
           "experiment": exp.get("id"), "target": exp.get("target"), "attempts": state.get("attempts", 0),
           "score": ((state.get("rerun") or {}).get("comparison") or {}).get("score"), "stub": True}
    log("report", f"outcome={out} attempts={rpt['attempts']}")
    return {"report": rpt}


NODES = {fnc.__name__: fnc for fnc in (
    understand, chaos_agent, guard_approve, inject, observe, diagnosis_agent, remediation_agent, apply_fix, verify,
    guard_revert, harden_agent, apply_hardening, rerun, compare, report)}


def build(nodes=None):
    """Compile the graph. nodes = {name: fn(state, config)} replaces those stubs (e.g. with real implementations)."""
    bad = sorted(set(nodes or {}) - set(NODES))
    if bad:
        raise ValueError(f"unknown nodes {bad}; known: {sorted(NODES)}")
    fns = {**NODES, **(nodes or {})}
    grf = StateGraph(HealState)
    for nam, fnc in fns.items():
        grf.add_node(nam, fnc)
    grf.add_edge(START, "understand")
    grf.add_edge("understand", "chaos_agent")
    grf.add_edge("chaos_agent", "guard_approve")
    grf.add_conditional_edges("guard_approve", route_approval, {"rejected": "report", "approved": "inject"})
    for src, dst in (("inject", "observe"), ("observe", "diagnosis_agent"), ("diagnosis_agent", "remediation_agent"),
                     ("remediation_agent", "apply_fix"), ("apply_fix", "verify")):
        grf.add_edge(src, dst)
    grf.add_conditional_edges("verify", route_verify,
                              {"healed": "harden_agent", "retry": "observe", "give_up": "guard_revert"})
    for src, dst in (("harden_agent", "apply_hardening"), ("apply_hardening", "rerun"), ("rerun", "compare"),
                     ("compare", "guard_revert"), ("guard_revert", "report")):
        grf.add_edge(src, dst)
    grf.add_edge("report", END)
    return grf.compile()


def run(prompt, scenario="retry", nodes=None):
    """Run the graph once. Return (final_state, path) where path lists the nodes in the order they ran."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario '{scenario}' not one of {SCENARIOS}")
    sta, pth = {"prompt": prompt, "attempts": 0}, []
    cfg = {"configurable": {"scenario": scenario}, "recursion_limit": 50}
    for upd in build(nodes).stream(dict(sta), config=cfg, stream_mode="updates"):
        for nam, val in upd.items():
            pth.append(nam)
            sta.update(val or {})
    return sta, pth
