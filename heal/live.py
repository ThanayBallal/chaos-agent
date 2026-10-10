"""Live nodes for heal.graph on the real compose project robot-shop (python -m heal.run "..." --live).

graph.run(..., live=True) puts NODES in place of the stubs of the same name and switches heal/ports.py to live;
chaos_agent, remediation_agent and the blind half of diagnosis_agent are the shared LLM agents (heal/agents.py, rule
fallbacks); harden_agent, apply_hardening, compare and revert_patch stay shared too.
Safety while healing: guard.approve (allowlist, depmap, live container), guard.ready before every injection, then
ports.heal_check: kill switch, collateral outside the blast radius, and a hard HEAL_S deadline from injection.
The error-rate and p95 aborts are off while healing (they are the symptoms being healed).
Results and evidence are measured only; a field that was not measured is None.
"""
import json
import time

from agent import analyst
from heal import graph, ports
from heal.graph import log
from monitor import baseline, live_check, runner
from safety import guard
from safety.allowlist import MAX_DUR

HEAL_S = MAX_DUR  # hard heal deadline, seconds from injection
VERIFY_S = 20  # max seconds one verify waits for steady state (leaves time for a second attempt)
READY_S = 30  # max seconds to wait for steady state before injecting
DEP = graph.DAT / "depmap.json"
MSD = ("window_s", "requests", "errors", "p50_ms", "endpoints", "health", "unhealthy", "containers", "cpu_pct", "netem",
       "logs")


# ---- helpers ----

def _stop(hel):
    """Abort reason that needs no sample: kill switch or the heal deadline."""
    if guard.kill_switch():
        return "kill switch (STOP file)"
    if time.monotonic() >= hel["until"]:
        return f"heal deadline {HEAL_S}s reached"
    return None


def _steady(bas):
    """Wait up to READY_S for guard.ready on 1 s samples. Return None when steady, else the last reason."""
    end, why = time.monotonic() + READY_S, "no sample taken"
    while time.monotonic() < end and not guard.kill_switch():
        why = guard.ready(baseline.sample(1.0), bas)
        if why is None:
            return None
    return "kill switch (STOP file)" if guard.kill_switch() else why


def _start(exp, bas):
    """Steady-state check -> cart probe -> ports.inject. Return (heal dict, abort reason or None)."""
    guard.reset()
    why = _steady(bas)
    if why:
        return {}, f"not injected: {why}"
    cid = f"chaos-heal-{exp['id']}-{int(time.time())}"
    sku, pnr = runner._probe_create(cid)
    sta = runner._now()
    res = ports.inject(exp)
    t_0 = time.monotonic()
    hel = {"t0": t_0, "until": t_0 + HEAL_S, "cid": cid, "sku": sku, "probe_error": pnr, "started_at": sta,
           "info": res.get("info"), "injected": res["injected"]}
    if not res["injected"]:
        return hel, f"injection failed: {res['error']}"
    return hel, None


def _result(exp, msd, hel):
    """result.json-shaped dict from one observation; recovery_s / data_survived are filled in after healing."""
    obs = ([f"fault applied: {hel['info']}"] if hel.get("info") else []) + list(msd["observations"])
    return {"id": exp["id"], "target": exp["target"], "fault": exp["fault"], "started_at": hel.get("started_at"),
            "ended_at": None, "error_rate": msd["error_rate"], "p95_ms": msd["p95_ms"], "recovery_s": None,
            "aborted": False, "abort_reason": None, "data_survived": None, "observations": obs,
            "measured": {key: msd[key] for key in MSD if key in msd}}


def _probe(hel):
    """Read the test cart back after recovery -> (data_survived, observation line)."""
    if not hel.get("sku"):
        return None, f"cart probe not run: {hel.get('probe_error')}"
    srv, det = runner._probe_read(hel["cid"], hel["sku"])
    if srv is None:
        return None, f"cart probe: test cart {hel['cid']} could not be read back after recovery ({det})"
    return srv, (f"cart probe: test cart {hel['cid']} created before the fault was "
                 f"{'still there' if srv else 'gone'} after recovery ({det}), data_survived={str(srv).lower()}")


def _drop_probe(hel):
    if hel and (hel.get("sku") or hel.get("probe_error")):
        runner._probe_delete(hel["cid"])


def _healed(res, ver, hel):
    """Complete a before/after result once steady state is back."""
    srv, lin = _probe(hel)
    return {**res, "recovery_s": ver["recovery_s"], "data_survived": srv, "ended_at": runner._now(),
            "observations": res["observations"] + [
                f"steady again {ver['recovery_s']}s after injection (guard.ready on {runner.REC_OK} "
                f"1 s samples in a row)", lin]}


def _findings(dep, bas, res, exp):
    """Rule-based findings from the measured result only (agent.analyst.fallback)."""
    if res.get("error_rate") is None:
        return {"resilience_score": None, "findings": []}
    return analyst.fallback(dep, bas, [res], [exp])


def _unstub(fnc):
    """Run a shared rule-based node and drop the 'stub' markers it adds."""
    def node(state, config):
        out = fnc(state, config)
        return {key: ({k: v for k, v in val.items() if k != "stub"} if isinstance(val, dict) else val)
                for key, val in out.items()}
    node.__name__ = fnc.__name__
    return node


def close():
    """End of a live run: revert injected faults, stop traffic, ports back to stub."""
    try:
        ports.revert_all()
    finally:
        baseline.stop()
        ports.use_live(False)


# ---- nodes ----

def understand(state, config):
    """depmap (data/depmap.json, else mapper), a fresh measured baseline, and the real code review."""
    if DEP.exists():
        dep = json.loads(DEP.read_text())
    else:
        from mapper.mapper import build_depmap
        dep = build_depmap()
    win = graph._knob(config, "baseline_s", 30)
    log("understand", f"capturing {win}s baseline under load...")
    bas = baseline.capture(win)
    try:
        mdl = ports.review(dep)
    except Exception as err:
        mdl = {"source": "unavailable", "risks": [], "error": str(err)[:200]}
    log("understand", f"baseline error_rate={bas['error_rate']} p95={bas['p95_ms']}ms; code review "
                      f"source={mdl.get('source')} risks={len(mdl.get('risks', []))}")
    return {"depmap": dep, "baseline": bas, "app_model": mdl}


def guard_approve(state, config):
    exp = state["experiment"]
    guard.reset()
    rej = ports.approve(exp, state["depmap"])
    log("guard_approve", f"rejected: {rej}" if rej else "approved")
    return {"experiment": {**exp, "rejection": rej}}


def inject(state, config):
    exp = state["experiment"]
    hel, abr = _start(exp, state["baseline"])
    log("inject", abr or f"{exp['fault']} on {exp['target']}: {hel['info']}")
    return {"heal": hel, "abort": abr,
            "symptoms": {"id": exp["id"], "target": exp["target"], "fault": exp["fault"], "injected": not abr}}


def observe(state, config):
    """Blind measurement (ports.observe) + the experiment's identity; then the live heal guard."""
    exp, hel, bas = state["experiment"], state["heal"], state["baseline"]
    sym = _result(exp, ports.observe(bas), hel)
    abr = ports.heal_check(sym["measured"], bas, exp["target"], hel["until"])
    if abr:
        sym.update(aborted=True, abort_reason=abr)
    log("observe", f"attempt {state.get('attempts', 0)}: error_rate={sym['error_rate']} p95={sym['p95_ms']}ms"
                   + (f" ABORT: {abr}" if abr else ""))
    return {"symptoms": sym, "observed": list(state.get("observed") or []) + [sym], "abort": abr}


def diagnosis_agent(state, config):
    """Shared blind diagnosis (graph.diagnosis_agent: symptoms + depmap only, scored afterwards), with findings for
    harden_agent from analyst.fallback on the measured result (post-hoc, never shown to the diagnosis)."""
    out = _unstub(graph.diagnosis_agent)(state, config)
    if out.get("diagnosis"):
        out["diagnosis"]["findings"] = _findings(state["depmap"], state["baseline"], state["symptoms"],
                                                 state["experiment"])
    return out


def apply_fix(state, config):
    """Run the heal actions, stopping at the deadline or kill switch (then -> verify -> give_up)."""
    act = graph.check_heal((state.get("fix_plan") or {}).get("actions") or [])
    att, hel, res, abr = state.get("attempts", 0) + 1, state["heal"], [], None
    for itm in act:
        abr = _stop(hel)
        if abr:
            break
        res.append(ports.heal_fix(itm["action"], itm["target"]))
    log("apply_fix", f"attempt {att}: " + ", ".join(f"{out['action']} ok={out['ok']}" for out in res)
                     + (f" ABORT: {abr}" if abr else ""))
    out = {"applied_fixes": list(state.get("applied_fixes") or []) + graph.fix_rows(act, res, att), "attempts": att}
    if abr:
        out["abort"] = abr
    return out


def verify(state, config):
    """ports.verify (guard.ready + recovery timing). Healed: finish the before-result (first observation, recovery_s,
    cart probe) and refresh the findings from it."""
    att, hel, exp, bas = state.get("attempts", 0), state["heal"], state["experiment"], state["baseline"]
    if state.get("abort"):
        return {"verify": {"healed": False, "attempt": att, "reason": state["abort"]}}
    ver = ports.verify(bas, hel["t0"], min(hel["until"], time.monotonic() + VERIFY_S), exp["target"])
    out = {"verify": {**ver, "attempt": att}}
    if ver.get("abort"):
        out["abort"] = ver["abort"]
    elif not ver["healed"] and time.monotonic() >= hel["until"]:
        out["abort"] = f"heal deadline {HEAL_S}s reached: {ver['reason']}"
    if ver["healed"]:
        bef = _healed((state.get("observed") or [state["symptoms"]])[0], ver, hel)
        out["symptoms"] = bef
        out["diagnosis"] = {**state["diagnosis"], "findings": _findings(state["depmap"], bas, bef, exp)}
    log("verify", f"attempt {att}: " + (f"healed, recovery {ver['recovery_s']}s" if ver["healed"]
                                       else f"not healed: {out.get('abort') or ver['reason']}"))
    return out


def trial(exp, bas):
    """Replay the heal measurement once: steady -> probe -> inject -> observe -> heal_plan via heal_fix -> verify ->
    probe -> revert. Return a result.json dict measured exactly like the heal phase's before-result."""
    hel, abr, res, ver = {}, None, None, {"healed": False, "recovery_s": None}
    try:
        hel, abr = _start(exp, bas)
        if not abr:
            res = _result(exp, ports.observe(bas), hel)
            abr = ports.heal_check(res["measured"], bas, exp["target"], hel["until"])
        if not abr:
            for itm in graph.heal_plan(exp["fault"], exp["target"]):
                abr = _stop(hel)
                if abr:
                    break
                fix = ports.heal_fix(itm["action"], itm["target"])
                res["observations"].append(f"heal {itm['action']}({itm['target']}) ok={fix['ok']}: {fix['detail']}")
        if not abr:
            ver = ports.verify(bas, hel["t0"], hel["until"], exp["target"])
            abr = ver.get("abort") or (None if ver["healed"] else f"not steady by the deadline: {ver['reason']}")
        if ver["healed"]:
            res = _healed(res, ver, hel)
    finally:
        ports.revert_all()
        _drop_probe(hel)
    if res is None:
        return {"id": exp["id"], "target": exp["target"], "fault": exp["fault"], "started_at": hel.get("started_at"),
                "ended_at": runner._now(), "error_rate": None, "p95_ms": None, "recovery_s": None, "aborted": True,
                "abort_reason": abr, "data_survived": None, "observations": [abr]}
    if abr:
        res = {**res, "aborted": True, "abort_reason": abr, "ended_at": runner._now(),
               "observations": res["observations"] + [f"aborted: {abr}"]}
    return res


def rerun(state, config):
    """Same experiment on the hardened app, measured like the heal phase -> rsl_after + fnd_after."""
    exp, bas, rnd = state["experiment"], state["baseline"], state["round"]
    res = trial(exp, bas)
    fnd = _findings(state["depmap"], bas, res, exp)
    log("rerun", f"round {rnd}: error_rate={res['error_rate']} recovery={res['recovery_s']}s "
                 f"data_survived={res['data_survived']}" + (f" aborted: {res['abort_reason']}" if res["aborted"] else ""))
    return {"rerun": {"round": rnd, "aborted": res["aborted"]}, "rsl_after": [res], "fnd_after": fnd}


def guard_revert(state, config):
    """Revert injected faults only (ports.revert_all), drop the probe cart."""
    ports.revert_all()
    hel, exp = state.get("heal") or {}, state["experiment"]
    _drop_probe(hel)
    rev = [f"fault {exp['id']} {exp['fault']} on {exp['target']}"] if hel.get("injected") else []
    log("guard_revert", f"reverted {', '.join(rev) or 'nothing (no fault injected)'}")
    return {"report": {**(state.get("report") or {}), "reverted_faults": rev}}


NODES = {fnc.__name__: fnc for fnc in (
    understand, _unstub(graph.chaos_agent), guard_approve, inject, observe, diagnosis_agent,
    _unstub(graph.remediation_agent),
    apply_fix, verify, rerun, guard_revert)}
