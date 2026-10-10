"""Self-healing chaos loop as a LangGraph StateGraph.

Heal (undo the injected fault now, at most MAX_ATTEMPTS):
  understand -> chaos_agent -> guard_approve -> inject -> observe -> diagnosis_agent -> remediation_agent
  -> apply_fix -> verify -> (healed) harden | (not healed, attempts < 2) observe | (otherwise) guard_revert -> report
  guard_approve -> (rejected) report.
Harden (make the app survive the fault next time, at most MAX_ROUNDS):
  harden_agent -> apply_hardening -> rerun -> compare -> (done: improved, or round 2 used) guard_revert -> report
                                                     -> (not improved after round 1) revert_patch -> harden_agent

remediation_agent / apply_fix may only use HEAL_CATALOG actions; hardening patches (patcher.make output) only
appear in harden_agent / apply_hardening. revert_patch only runs between round 1 and round 2.
guard_revert undoes injected faults only, never patches.
chaos_agent, diagnosis_agent and remediation_agent are LLM agents (heal/agents.py) with rule-based fallbacks; which
one answered is kept per call in state["agents"]. diagnosis_agent is blind (symptoms + depmap only) and is scored
against the experiment afterwards (state["diagnosis_score"]). On a retry the first diagnosis stands and
remediation_agent gets the previous diagnosis, the actions taken and the new symptoms.
Abort (live only: not steady, injection failed, kill switch, collateral outside the blast radius, 60 s heal deadline):
  inject / observe -> (abort) guard_revert; verify -> (abort) give_up -> guard_revert.

Two modes, one graph. STUB (default): the nodes below log and return plausible fake data marked "stub": True and never
write data/*.json. LIVE (run(..., live=True)): heal/live.py replaces the measuring nodes with real ones on project
robot-shop and heal/ports.py switches to the real modules. Nodes call team modules only through heal/ports.py.
Stub knobs in config["configurable"]: scenario = retry (default) | healed | unhealed | rejected (heal outcome),
harden = round1 (default) | round2 | never (which hardening round improves). Both are ignored in live mode.
"""
import json
import time
from pathlib import Path
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from heal import agents, ports
from heal.agents import HEAL, HEAL_CATALOG, HEAL_DESC, WEB, check_heal, heal_plan  # noqa: F401 (re-exported)

DAT = Path(__file__).resolve().parent.parent / "data"
MAX_ATTEMPTS = 2  # contracts.md: at most 2 fix attempts per run
MAX_ROUNDS = 2  # hardening rounds
SCENARIOS = ("retry", "healed", "unhealed", "rejected")
HARDEN = ("round1", "round2", "never")
LLM_S = 15  # live: max seconds per heal-agent LLM call (diagnosis, remediation)
RESERVE_S = 25  # live: seconds of the heal deadline kept for apply_fix + verify


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
    round: int
    round1_patches: list
    rsl_after: list
    fnd_after: dict
    comparisons: list
    abort: str
    heal: dict
    observed: list
    agents: dict
    diagnosis_score: dict
    hardening_log: list


def log(nam, msg):
    print(f"[heal] {nam}: {msg}")


def _knob(config, key, dfl):
    return ((config or {}).get("configurable") or {}).get(key, dfl)


def _load(nam):
    return json.loads((DAT / nam).read_text())


# ---- routing (pure functions of the state) ----

def route_approval(state):
    """'rejected' if the guard gave a rejection reason, else 'approved'."""
    return "rejected" if (state.get("experiment") or {}).get("rejection") else "approved"


def route_abort(state):
    """'abort' if a live guard check stopped the heal (state["abort"] set), else 'ok'."""
    return "abort" if state.get("abort") else "ok"


def route_verify(state):
    """'healed' if verify says so; else 'give_up' on an abort; else 'retry' while attempts < MAX_ATTEMPTS;
    else 'give_up'."""
    if (state.get("verify") or {}).get("healed"):
        return "healed"
    if state.get("abort"):
        return "give_up"
    return "retry" if state.get("attempts", 0) < MAX_ATTEMPTS else "give_up"


def improved(cmp):
    return any(exp.get("improved") for exp in (cmp or {}).get("experiments", []))


def route_compare(state):
    """'round2' if the latest comparison did not improve and round < MAX_ROUNDS; else 'done' (improved, or both
    rounds used). One label for both endings: LangGraph draws one edge per target, so a second label would vanish."""
    if improved((state.get("comparisons") or [None])[-1]):
        return "done"
    return "round2" if state.get("round", 0) < MAX_ROUNDS else "done"


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


def _agents(state, nam, met):
    """state["agents"] with one more call of agent nam: {"source": "llm"|"fallback", "why"}."""
    out = {key: list(val) for key, val in (state.get("agents") or {}).items()}
    out.setdefault(nam, []).append(met)
    return out


def _cap(state):
    """Live: seconds a heal agent's LLM call may take, keeping RESERVE_S of the heal deadline for apply_fix + verify.
    Stub (no deadline): None = ask_json's default cap."""
    unt = (state.get("heal") or {}).get("until")
    return None if unt is None else min(LLM_S, unt - time.monotonic() - RESERVE_S)


def chaos_agent(state, config: RunnableConfig):
    """LLM: one experiment from the prompt (what it names wins), the code-review risks and the depmap; guard.approve
    rejections are fed back to the LLM. Fallback: the prompt's target/fault, else the top code-review risk."""
    exp, met = agents.plan_experiment(state.get("prompt") or "", (state.get("app_model") or {}).get("risks", []),
                                      state["depmap"])
    exp["stub"] = True
    log("chaos_agent", f"[{met['source']}] {exp['fault']} {exp['target']} {exp['params']}: {exp['reason'][:120]}")
    return {"experiment": exp, "agents": _agents(state, "chaos_agent", met)}


def guard_approve(state, config: RunnableConfig):
    """STUB: approve unless the scenario is 'rejected'."""
    rej = "STUB: rejected by scenario" if _knob(config, "scenario", "retry") == "rejected" else None
    log("guard_approve", f"rejected: {rej}" if rej else "approved")
    return {"experiment": {**state["experiment"], "rejection": rej}}


def inject(state, config: RunnableConfig):
    """STUB: pretend the fault was injected."""
    exp = state["experiment"]
    log("inject", f"{exp['fault']} on {exp['target']} (not really)")
    return {"symptoms": {"id": exp["id"], "target": exp["target"], "fault": exp["fault"], "injected": True,
                         "stub": True}}


def _stub_measured(exp, bas):
    """STUB world: what ports.observe would measure while exp's fault is active (the stub may know the fault; the
    diagnosis agent still only sees agents.brief of it)."""
    tgt, fal = exp["target"], exp["fault"]
    hlt = {svc: "healthy" for svc in bas.get("health", {})}
    msd = {"health": hlt, "containers": {svc: "running" for svc in hlt}, "cpu_pct": {}, "netem": [], "logs": {},
           "endpoints": {}}
    if fal == "kill":
        hlt[tgt], msd["containers"][tgt] = "unhealthy", "exited"
        msd["logs"][tgt] = ["STUB: container exited with code 137"]
    elif fal == "pause":
        hlt[tgt] = msd["containers"][tgt] = "paused"
    elif fal == "latency":
        msd["netem"] = [tgt]
    else:
        msd["cpu_pct"][tgt] = 190.0
    msd["unhealthy"] = sorted(svc for svc, sta in hlt.items() if sta != "healthy")
    return msd


def observe(state, config: RunnableConfig):
    """STUB: result.json-like symptoms plus "measured"; worse before any heal attempt, better after one."""
    exp, att = state["experiment"], state.get("attempts", 0)
    sym = {"id": exp["id"], "target": exp["target"], "fault": exp["fault"],
           "error_rate": 0.62 if att == 0 else 0.31, "p95_ms": 2400 if att == 0 else 900,
           "recovery_s": 14 if att == 0 else 8, "aborted": False, "abort_reason": None,
           "data_survived": False, "observations": [f"STUB: {exp['target']} dependents return 5xx"],
           "measured": _stub_measured(exp, state["baseline"]), "stub": True}
    log("observe", f"attempt {att}: error_rate={sym['error_rate']} p95={sym['p95_ms']}ms")
    return {"symptoms": sym}


def _stub_findings(state):
    """STUB findings.json for harden_agent (post-hoc analysis, not part of the blind diagnosis)."""
    exp, sym = state["experiment"], state["symptoms"]
    rsk = next((itm for itm in (state.get("app_model") or {}).get("risks", [])
                if itm.get("dependency") == exp["target"] and itm.get("fault") == exp["fault"]), None)
    return {"resilience_score": 50, "findings": [{
        "rank": 1, "severity": "critical", "target": exp["target"], "experiment": exp["id"],
        "weakness": rsk["weakness"] if rsk else f"single {exp['target']} instance with no fallback",
        "evidence": f"STUB: error_rate {state['baseline'].get('error_rate')} -> {sym['error_rate']}",
        "hypothesis_matched": True, "fix": rsk["fix"] if rsk else "add restart: unless-stopped + healthcheck"}]}


def diagnosis_agent(state, config: RunnableConfig):
    """BLIND LLM diagnosis: agents.diagnose sees agents.brief(symptoms, baseline) and the depmap, never the
    experiment; then it is scored against the experiment. findings (for harden_agent) are added separately.
    On a retry the first diagnosis stands (no LLM call): remediation_agent gets the new symptoms instead."""
    if state.get("attempts", 0) and (state.get("diagnosis") or {}).get("fault_guess"):
        log("diagnosis_agent", "retry: keeping the first diagnosis")
        return {}
    dx, met = agents.diagnose(agents.brief(state["symptoms"], state["baseline"]), state["depmap"], cap=_cap(state))
    scr = agents.score(dx, state["experiment"])
    why = f"{dx['fault_guess']} on {dx['suspect_service']}: " + "; ".join(dx["evidence"][:2])
    log("diagnosis_agent", f"[{met['source']}] {why} (confidence {dx['confidence']}, correct={scr['correct']})")
    return {"diagnosis": {**dx, "root_cause": why, "findings": _stub_findings(state), "stub": True},
            "diagnosis_score": scr, "agents": _agents(state, "diagnosis_agent", met)}


def remediation_agent(state, config: RunnableConfig):
    """LLM: HEAL_CATALOG actions that undo the diagnosed fault now (never hardening patches); restart_web is appended
    automatically after a backend start. Attempt 2 also gets the previous diagnosis, actions taken, new symptoms."""
    dx, att = state["diagnosis"], state.get("attempts", 0)
    his = None
    if att:
        his = {"previous_diagnosis": {key: dx.get(key) for key in agents.DX_KEYS},
               "actions_taken": [{key: itm.get(key) for key in ("attempt", "action", "target", "ok", "detail")}
                                 for itm in state.get("applied_fixes") or []],
               "new_symptoms": agents.brief(state["symptoms"], state["baseline"])}
    pln, met = agents.remediate(dx, state["depmap"], his, cap=_cap(state))
    act = check_heal(pln["actions"])
    log("remediation_agent", f"[{met['source']}] " + (" -> ".join(f"{itm['action']}({itm['target']})" for itm in act)
                                                     or "no action"))
    return {"fix_plan": {"actions": act, "reason": pln.get("reason"), "stub": True},
            "agents": _agents(state, "remediation_agent", met)}


def apply_fix(state, config: RunnableConfig):
    """Run each heal action: heal_fix(action, target). Refuses anything outside HEAL_CATALOG. Counts the attempt."""
    act = check_heal((state.get("fix_plan") or {}).get("actions") or [])
    att = state.get("attempts", 0) + 1
    res = [ports.heal_fix(itm["action"], itm["target"]) or {} for itm in act]
    log("apply_fix", f"attempt {att}: {', '.join(itm['action'] for itm in act)}")
    return {"applied_fixes": list(state.get("applied_fixes") or []) + fix_rows(act, res, att), "attempts": att}


def fix_rows(act, res, att):
    """applied_fixes entries: the planned action, its attempt number, and what heal_fix reported."""
    return [{**itm, "attempt": att, "ok": out.get("ok"), "detail": out.get("detail")} for itm, out in zip(act, res)]


def verify(state, config: RunnableConfig):
    """STUB: healed per scenario - healed: 1st attempt, retry: 2nd attempt, unhealed: never."""
    att, scn = state.get("attempts", 0), _knob(config, "scenario", "retry")
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
    """Round 1: make(fnd, pln, dep, rsl=rsl). Round 2: make(fnd_after, pln, dep, rsl=rsl_after, exclude=round1)."""
    rnd = state.get("round", 0) + 1
    if rnd > MAX_ROUNDS:
        raise RuntimeError(f"hardening round {rnd} > {MAX_ROUNDS}")
    pln, dep = [state["experiment"]], state["depmap"]
    if rnd == 1:
        pat = ports.make(state["diagnosis"]["findings"], pln, dep, rsl=[state["symptoms"]])
    else:
        pat = ports.make(state["fnd_after"], pln, dep, rsl=state["rsl_after"], exclude=state["round1_patches"])
    log("harden_agent", f"round {rnd}: {', '.join(itm['fix'] + ' ' + itm['service'] for itm in pat['patches'])}")
    return {"round": rnd, "hardening": {"round": rnd, "patch": pat}}


def apply_hardening(state, config: RunnableConfig):
    """Apply the round's patches live: setup.patch.apply(pat)."""
    hrd, rnd = state["hardening"], state["round"]
    ports.apply(hrd["patch"])
    ids = [itm["id"] for itm in hrd["patch"].get("patches", [])]
    log("apply_hardening", f"round {rnd}: applied {', '.join(ids) or 'nothing'}")
    out = {"hardening": {**hrd, "applied": ids},
           "hardening_log": list(state.get("hardening_log") or []) + [
               {"round": rnd, "patches": [f"{itm['fix']} {itm['service']}" for itm in hrd["patch"].get("patches", [])]}]}
    if rnd == 1:
        out["round1_patches"] = list(hrd["patch"].get("patches", []))
    return out


def rerun(state, config: RunnableConfig):
    """STUB: rerun the same experiment on the hardened app -> rsl_after (result.json list) + fnd_after."""
    exp, rnd, bef = state["experiment"], state["round"], state["symptoms"]
    oks = _knob(config, "harden", "round1") == f"round{rnd}"
    num = {"error_rate": 0.05, "p95_ms": 300, "recovery_s": 4, "data_survived": True} if oks else \
        {key: bef.get(key) for key in ("error_rate", "p95_ms", "recovery_s", "data_survived")}
    res = {"id": exp["id"], "target": exp["target"], "fault": exp["fault"], **num, "aborted": False,
           "abort_reason": None, "observations": [f"STUB: rerun after hardening round {rnd}"], "stub": True}
    fnd = {"resilience_score": 78, "findings": [], "stub": True} if oks else \
        {**state["diagnosis"]["findings"], "stub": True}
    log("rerun", f"round {rnd}: {exp['fault']} {exp['target']} error_rate={res['error_rate']}")
    return {"rerun": {"round": rnd, "stub": True}, "rsl_after": [res], "fnd_after": fnd}


def compare(state, config: RunnableConfig):
    """Always against the original before-results: compare(rsl, rsl_after, fnd, fnd_after)."""
    cmp = ports.compare([state["symptoms"]], state["rsl_after"], state["diagnosis"]["findings"], state["fnd_after"])
    log("compare", f"round {state['round']}: improved={improved(cmp)}")
    return {"comparisons": list(state.get("comparisons") or []) + [cmp]}


def revert_patch(state, config: RunnableConfig):
    """Undo the round-1 patches before round 2: setup.patch.revert(). Only legal between round 1 and round 2."""
    if state.get("round") != 1:
        raise RuntimeError(f"revert_patch only runs between round 1 and round 2 (round={state.get('round')})")
    ports.revert_patch()
    log("revert_patch", f"undid round-1 patches {', '.join(itm['id'] for itm in state.get('round1_patches') or [])}")
    return {"hardening": {**state["hardening"], "reverted": True}}


def report(state, config: RunnableConfig):
    """Summarise the run: prompt -> experiment -> blind diagnosis (correct?) -> heal actions -> healed in Xs ->
    hardening rounds -> which agents used the LLM and which the fallback."""
    exp, cmp, dxs = state.get("experiment") or {}, state.get("comparisons") or [], state.get("diagnosis") or {}
    if exp.get("rejection"):
        out = "rejected"
    elif (state.get("verify") or {}).get("healed"):
        out = "healed"
    elif state.get("abort"):
        out = "aborted"
    else:
        out = "not_healed"
    hrd = next((f"improved_round{idx}" for idx, itm in enumerate(cmp, 1) if improved(itm)),
               "not_improved" if cmp else None)
    rpt = {**(state.get("report") or {}), "prompt": state.get("prompt"), "outcome": out, "hardening": hrd,
           "experiment": exp.get("id"), "target": exp.get("target"), "attempts": state.get("attempts", 0),
           "rounds": state.get("round", 0), "comparisons": cmp, "abort": state.get("abort"),
           "applied_fixes": state.get("applied_fixes") or [],
           "score": cmp[-1]["score"] if cmp else None, "stub": not _knob(config, "live", False),
           "experiment_detail": {key: exp.get(key) for key in ("target", "fault", "params", "reason", "hypothesis",
                                                               "rejection")} if exp else None,
           "diagnosis": {key: dxs.get(key) for key in agents.DX_KEYS} if dxs.get("fault_guess") else None,
           "diagnosis_correct": (state.get("diagnosis_score") or {}).get("correct"),
           "diagnosis_score": state.get("diagnosis_score"),
           "healed_in_s": (state.get("verify") or {}).get("recovery_s") if out == "healed" else None,
           "hardening_rounds": [{**itm, "improved": improved(cmp[idx]) if idx < len(cmp) else None}
                                for idx, itm in enumerate(state.get("hardening_log") or [])],
           "agents": {nam: [met["source"] for met in mets] for nam, mets in (state.get("agents") or {}).items()},
           "agents_detail": state.get("agents") or {}}
    log("report", f"outcome={out} hardening={hrd} attempts={rpt['attempts']} rounds={rpt['rounds']}")
    return {"report": rpt}


NODES = {fnc.__name__: fnc for fnc in (
    understand, chaos_agent, guard_approve, inject, observe, diagnosis_agent, remediation_agent, apply_fix, verify,
    guard_revert, harden_agent, apply_hardening, rerun, compare, revert_patch, report)}


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
    grf.add_conditional_edges("inject", route_abort, {"abort": "guard_revert", "ok": "observe"})
    grf.add_conditional_edges("observe", route_abort, {"abort": "guard_revert", "ok": "diagnosis_agent"})
    for src, dst in (("diagnosis_agent", "remediation_agent"), ("remediation_agent", "apply_fix"),
                     ("apply_fix", "verify")):
        grf.add_edge(src, dst)
    grf.add_conditional_edges("verify", route_verify,
                              {"healed": "harden_agent", "retry": "observe", "give_up": "guard_revert"})
    for src, dst in (("harden_agent", "apply_hardening"), ("apply_hardening", "rerun"), ("rerun", "compare"),
                     ("revert_patch", "harden_agent"), ("guard_revert", "report")):
        grf.add_edge(src, dst)
    grf.add_conditional_edges("compare", route_compare,
                              {"done": "guard_revert", "round2": "revert_patch"})
    grf.add_edge("report", END)
    return grf.compile()


def run(prompt, scenario="retry", nodes=None, harden="round1", live=False, baseline_s=30):
    """Run the graph once. Return (final_state, path) where path lists the nodes in the order they ran.
    live=True: real nodes from heal/live.py (nodes= still overrides them), real ports, baseline_s s baseline;
    injected faults are reverted and traffic stopped when the run ends, however it ends."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario '{scenario}' not one of {SCENARIOS}")
    if harden not in HARDEN:
        raise ValueError(f"harden '{harden}' not one of {HARDEN}")
    sta, pth = {"prompt": prompt, "attempts": 0}, []
    cfg = {"configurable": {"scenario": scenario, "harden": harden, "live": live, "baseline_s": baseline_s},
           "recursion_limit": 60}
    lmd = None
    if live:
        from heal import live as lmd
        ports.use_live(True)
        nodes = {**lmd.NODES, **(nodes or {})}
    try:
        for upd in build(nodes).stream(dict(sta), config=cfg, stream_mode="updates"):
            for nam, val in upd.items():
                pth.append(nam)
                sta.update(val or {})
    finally:
        if lmd:
            lmd.close()
    return sta, pth
