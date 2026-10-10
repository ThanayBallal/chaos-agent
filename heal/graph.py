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
Abort (live only: not steady, injection failed, kill switch, collateral outside the blast radius, 60 s heal deadline):
  inject / observe -> (abort) guard_revert; verify -> (abort) give_up -> guard_revert.

Two modes, one graph. STUB (default): the nodes below log and return plausible fake data marked "stub": True and never
write data/*.json. LIVE (run(..., live=True)): heal/live.py replaces the measuring nodes with real ones on project
robot-shop and heal/ports.py switches to the real modules. Nodes call team modules only through heal/ports.py.
Stub knobs in config["configurable"]: scenario = retry (default) | healed | unhealed | rejected (heal outcome),
harden = round1 (default) | round2 | never (which hardening round improves). Both are ignored in live mode.
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
MAX_ROUNDS = 2  # hardening rounds
SCENARIOS = ("retry", "healed", "unhealed", "rejected")
HARDEN = ("round1", "round2", "never")
WEB = "web"  # nginx proxy: caches backend IPs, so it is restarted after any backend restart
HEAL = {"kill": "start_service", "pause": "unpause_service", "latency": "remove_latency",
        "cpu_stress": "stop_cpu_stress"}
HEAL_CATALOG = (*HEAL.values(), "restart_web")
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
    round: int
    round1_patches: list
    rsl_after: list
    fnd_after: dict
    comparisons: list
    abort: str
    heal: dict
    observed: list


def log(nam, msg):
    print(f"[heal] {nam}: {msg}")


def _knob(config, key, dfl):
    return ((config or {}).get("configurable") or {}).get(key, dfl)


def _load(nam):
    return json.loads((DAT / nam).read_text())


# ---- heal catalog ----

def heal_plan(fault, target):
    """Catalog actions that undo `fault` on `target`; restart_web last whenever a backend was restarted."""
    act = [{"action": HEAL[fault], "target": target}]
    if HEAL[fault] == "start_service" and target != WEB:
        act.append({"action": "restart_web", "target": WEB})
    return act


def check_heal(act):
    """Raise ValueError unless every action is in HEAL_CATALOG and restart_web is last after a backend restart."""
    for itm in act:
        if itm.get("action") not in HEAL_CATALOG:
            raise ValueError(f"'{itm.get('action')}' is not a heal action {HEAL_CATALOG}; "
                             f"hardening patches belong in harden_agent")
    web = [idx for idx, itm in enumerate(act) if itm["action"] == "restart_web"]
    if any(itm["action"] == "start_service" and itm.get("target") != WEB for itm in act) and web != [len(act) - 1]:
        raise ValueError("restart_web must be the last action after a backend restart (nginx caches backend IPs)")
    if web and web != [len(act) - 1]:
        raise ValueError("restart_web may appear once, as the last action")
    return act


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
    rej = "STUB: rejected by scenario" if _knob(config, "scenario", "retry") == "rejected" else None
    log("guard_approve", f"rejected: {rej}" if rej else "approved")
    return {"experiment": {**state["experiment"], "rejection": rej}}


def inject(state, config: RunnableConfig):
    """STUB: pretend the fault was injected."""
    exp = state["experiment"]
    log("inject", f"{exp['fault']} on {exp['target']} (not really)")
    return {"symptoms": {"id": exp["id"], "target": exp["target"], "fault": exp["fault"], "injected": True,
                         "stub": True}}


def observe(state, config: RunnableConfig):
    """STUB: result.json-like symptoms; worse before any heal attempt, better after one."""
    exp, att = state["experiment"], state.get("attempts", 0)
    sym = {"id": exp["id"], "target": exp["target"], "fault": exp["fault"],
           "error_rate": 0.62 if att == 0 else 0.31, "p95_ms": 2400 if att == 0 else 900,
           "recovery_s": 14 if att == 0 else 8, "aborted": False, "abort_reason": None,
           "data_survived": False, "observations": [f"STUB: {exp['target']} dependents return 5xx"],
           "stub": True}
    log("observe", f"attempt {att}: error_rate={sym['error_rate']} p95={sym['p95_ms']}ms")
    return {"symptoms": sym}


def diagnosis_agent(state, config: RunnableConfig):
    """STUB: root cause plus a findings.json-shaped dict that patcher.make consumes."""
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
    """STUB: pick heal actions from HEAL_CATALOG that undo the injected fault now (never hardening patches)."""
    exp = state["experiment"]
    act = check_heal(heal_plan(exp["fault"], exp["target"]))
    log("remediation_agent", " -> ".join(f"{itm['action']}({itm['target']})" for itm in act))
    return {"fix_plan": {"actions": act, "stub": True}}


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
    out = {"hardening": {**hrd, "applied": ids}}
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
    """Summarise the run, with every hardening comparison."""
    exp, cmp = state.get("experiment") or {}, state.get("comparisons") or []
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
           "score": cmp[-1]["score"] if cmp else None, "stub": not _knob(config, "live", False)}
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
