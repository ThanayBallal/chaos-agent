"""Autonomous campaign: python -m heal.run --auto --all --live|--stub [--max N] [--pause-before-heal | --pause-first]

understand runs ONCE (depmap, baseline, the cached code review) and the Code review section is printed once; then
each ranked weakness #1..#N (N = min(--max, weaknesses)) gets one full heal cycle: graph.run(None, auto=True,
ctx=<understand result>, pick={"rank": k, "of": N}) -> chaos_agent forced to that weakness -> guard_approve -> inject
-> (hold) -> observe -> blind diagnosis -> remediation -> verify -> harden -> rerun -> compare -> guard_revert.
- A weakness the guard rejects is skipped (status "skipped" with the reason); the campaign goes on.
- Between cycles: ports.ready (guard.ready on 1 s samples) for up to READY_S; if not steady, the heal catalog's
  restart_web and one more READY_S wait; still not steady -> the campaign stops safely and says why.
- STOP file or Ctrl+C stops the whole campaign; graph.run always reverts the active fault, and the report is still
  written. Ctrl+C during a hold ends that cycle (graph.CTRL_C), then the campaign.
- Hardening patches still applied at the end of a cycle stay applied for the later cycles (ports.keep) and are listed
  in the report; they are undone by the guard at exit like in a single run.
- Holds: hold=True pauses before healing in every cycle; first=True only in cycle 1.
LLM quota: the code review is the cached one (unless --refresh); per cycle the agents use about 3 calls (chaos,
diagnosis, remediation), each with its rule-based fallback.
Live runs write data/heal_campaign.json (mode "auto-campaign": weaknesses, cycles with each report, kept patches,
summary) and data/heal_report.json = the last cycle that ran, after every cycle; stub runs write nothing unless
out / last are given.
"""
import json
import signal
import threading
import time
from datetime import datetime

from heal import agents, graph, ports
from heal.graph import log
from safety import guard

MAX_N = 4  # default --max
READY_S = 60  # max seconds to wait for steady state between cycles (twice: before and after restart_web)
CAMPAIGN = graph.DAT / "heal_campaign.json"
COLS = ("#", "weakness (file:line)", "target", "fault", "diagnosis correct?", "healed in", "hardening improved?",
        "score before -> after")


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _understand(live, baseline_s):
    """The understand node, once, outside the graph (live: real depmap, baseline and code review)."""
    cfg = {"configurable": {"live": live, "baseline_s": baseline_s}}
    if live:
        from heal import live as lmd
        return lmd.understand({}, cfg)
    return graph.understand({}, cfg)


def _wait(bas):
    """ports.ready until steady, for at most READY_S. -> None when steady, else the reason (kill switch wins)."""
    end, why = time.monotonic() + READY_S, "no sample taken"
    while True:
        if guard.kill_switch():
            return graph.KILL
        why = ports.ready(bas)
        if why is None or time.monotonic() >= end:
            return why


def between(bas):
    """Steady state before the next cycle: wait; if not steady, restart_web and wait once more.
    -> None when steady, else why the campaign must stop."""
    why = _wait(bas)
    if why in (None, graph.KILL):
        return why
    log("campaign", f"not steady after {READY_S}s ({why}): restart_web, then waiting once more")
    fix = ports.heal_fix("restart_web", agents.WEB) or {}
    why = _wait(bas)
    if why in (None, graph.KILL):
        return why
    return (f"not steady between cycles: {why} (after {READY_S}s, restart_web ok={fix.get('ok')} and another "
            f"{READY_S}s)")


def row(cyc):
    """One summary-table row of a cycle (values as measured / reported, None when not known)."""
    rpt = cyc.get("report") or {}
    hrd, scr = rpt.get("hardening"), rpt.get("score") or {}
    return {"rank": cyc["rank"], "where": cyc["where"], "target": cyc["target"], "fault": cyc["fault"],
            "status": cyc["status"], "outcome": rpt.get("outcome"), "diagnosis_correct": rpt.get("diagnosis_correct"),
            "healed": rpt.get("healed"), "healed_in_s": rpt.get("healed_in_s"), "hardening": hrd,
            "hardening_improved": None if hrd in (None, "not_applicable") else hrd.startswith("improved"),
            "score_before": scr.get("before"), "score_after": scr.get("after"),
            "note": cyc.get("skip_reason") or cyc.get("stop_reason")}


def _cells(itm):
    yes = {True: "yes", False: "no", None: "-"}
    if itm["status"] in ("skipped", "stopped"):
        return [str(itm["rank"]), itm["where"], itm["target"], itm["fault"], f"{itm['status']}: {itm['note']}"]
    if itm["healed"]:
        hld = f"{itm['healed_in_s']}s" if itm["healed_in_s"] is not None else "healed (not timed)"
    else:
        hld = f"not healed ({itm['outcome']})"
    hrd = yes[itm["hardening_improved"]] + (f" ({itm['hardening']})" if itm["hardening"] else "")
    scr = "-" if itm["score_before"] is None and itm["score_after"] is None else \
        f"{itm['score_before']} -> {itm['score_after']}"
    return [str(itm["rank"]), itm["where"], itm["target"], itm["fault"], yes[itm["diagnosis_correct"]], hld, hrd, scr]


def table(rpt):
    """Print the campaign summary table (a skipped weakness spans the remaining columns)."""
    rows, wid = [_cells(itm) for itm in rpt["summary"]], [len(col) for col in COLS]
    for cel in rows:
        for idx, val in enumerate(cel if len(cel) == len(COLS) else cel[:-1]):  # a spanning note sets no width
            wid[idx] = max(wid[idx], len(val))
    print(f"=== Campaign summary: {len(rpt['cycles'])} of {rpt['n']} cycles"
          + (f", stopped: {rpt['stopped']}" if rpt.get("stopped") else "") + " ===")
    for cel in [list(COLS), ["-" * num for num in wid]] + rows:
        print(" | ".join(val if idx == len(cel) - 1 else val.ljust(wid[idx]) for idx, val in enumerate(cel)))
    if rpt.get("not_run"):
        print(f"   not run: weakness {', '.join('#' + str(num) for num in rpt['not_run'])}")
    kpt = rpt.get("hardening_kept") or []
    print("== Hardening kept applied: " + (", ".join(f"{itm['id']} {itm['fix']} {itm['service']} (cycle {itm['cycle']})"
                                                     for itm in kpt) or "none"))


def _save(rpt, out):
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rpt, indent=2, default=str), encoding="utf-8")


def _sigint():
    """Ctrl+C raises KeyboardInterrupt during the campaign (guard.install's handler would exit past the report).
    -> the previous handler, or None off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return None
    old = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    return old


def run(live=False, scenario="retry", harden="round1", baseline_s=30, max_n=MAX_N, hold=False, first=False,
        out=None, last=None, each=None):
    """Run the campaign. -> the campaign report (also written to out; last gets the last cycle that ran, as
    heal_report.json). each(report) is called after every cycle that ran (heal.run prints it)."""
    if max_n < 1:
        raise ValueError("max_n must be >= 1")
    rpt = {"mode": "auto-campaign", "started_at": _now(), "ended_at": None, "max": max_n, "n": 0, "live": live,
           "code_review": None, "baseline": None, "weaknesses": [], "cycles": [], "not_run": [], "hardening_kept": [],
           "stopped": None, "summary": []}
    old, kpt = _sigint(), []
    ports.use_live(live)
    try:
        ctx = _understand(live, baseline_s)
        mdl, bas = ctx["app_model"], ctx["baseline"]
        wks = agents.weaknesses(mdl.get("risks", []), ctx["depmap"])
        num = min(max_n, len(wks))
        rpt.update(n=num, weaknesses=wks, not_run=[wek["rank"] for wek in wks[:num]],
                   code_review={key: mdl.get(key) for key in ("source", "files_read")},
                   baseline={key: bas.get(key) for key in ("error_rate", "p95_ms", "captured_at")})
        graph.show_weaknesses(wks, mdl)
        print(f"== Mode: auto campaign: weaknesses #1..#{num} of {len(wks)}, one heal cycle each")
        if not wks:
            rpt["stopped"] = "no code-review weakness could be read from the source"
        ctx = {**ctx, "weaknesses": wks}
        for wek in wks[:num]:
            k = wek["rank"]
            ports.use_live(live)
            why = graph.KILL if guard.kill_switch() else (between(bas) if k > 1 else None)
            if why:
                rpt["stopped"] = why
                log("campaign", f"stopping before cycle {k}/{num}: {why}")
                break
            print(f"=== Cycle {k}/{num}: {wek['dependency']} {wek['fault']} ===", flush=True)
            ports.campaign(k)
            rpt["not_run"].remove(k)
            cyc = {"cycle": k, "rank": k, "where": f"{wek['file']}:{wek['line']}", "service": wek["service"],
                   "target": wek["dependency"], "fault": wek["fault"], "weakness": wek["weakness"], "status": "running"}
            rpt["cycles"].append(cyc)
            sta, pth = graph.run(None, scenario, harden=harden, live=live, baseline_s=baseline_s,
                                 hold=(hold and not first) or (first and k == 1), auto=True, ctx=ctx,
                                 pick={"rank": k, "of": num}, save=False)
            cyc_rpt = sta.get("report") or {}
            cyc.update(report={**cyc_rpt, "path": pth}, kept_patches=_keep(k, kpt))
            if cyc_rpt.get("outcome") == "rejected":
                cyc.update(status="skipped", skip_reason=(cyc_rpt.get("experiment_detail") or {}).get("rejection"))
                print(f"== Cycle {k}/{num}: skipped weakness #{k}: {cyc['skip_reason']}")
            else:
                cyc["status"] = "ran"
                if each:
                    each(cyc_rpt)
                if last:
                    graph.save_report(cyc_rpt, pth, last)
            rpt["summary"] = [row(itm) for itm in rpt["cycles"]]
            _save(rpt, out)
            if cyc_rpt.get("abort") == graph.CTRL_C or guard.kill_switch():
                rpt["stopped"] = graph.CTRL_C if cyc_rpt.get("abort") == graph.CTRL_C else graph.KILL
                log("campaign", f"stopping after cycle {k}/{num}: {rpt['stopped']}")
                break
    except KeyboardInterrupt:
        rpt["stopped"] = "Ctrl+C"
        log("campaign", "Ctrl+C: stopping the campaign, active fault reverted")
    except Exception as err:
        rpt["stopped"] = f"error: {type(err).__name__}: {err}"[:300]
        log("campaign", f"stopped by an error: {rpt['stopped']}")
    finally:
        _end(rpt, kpt, live, old)
        _save(rpt, out)
    return rpt


def _keep(k, kpt):
    """Patches cycle k left applied: kept for the later cycles. -> their ids, fix, service."""
    new = [{"cycle": k, **{key: itm.get(key) for key in ("id", "fix", "service", "experiment")}} for itm in ports.keep()]
    kpt.extend(new)
    return new


def _end(rpt, kpt, live, old):
    """Revert any active fault, finish the cycle that was cut off, list the patches still kept, restore Ctrl+C."""
    try:
        if live:
            from heal import live as lmd
            ports.use_live(True)
            lmd.close()  # injected faults only (graph.run already did on its way out), then traffic off
        else:
            ports.revert_all()
    finally:
        cur = rpt["cycles"][-1] if rpt["cycles"] else None
        if cur and cur["status"] == "running":
            cur.update(status="stopped", stop_reason=rpt["stopped"], report=None,
                       kept_patches=_keep(cur["rank"], kpt))
        ids = {itm["id"] for itm in ports.kept()}
        rpt.update(hardening_kept=[itm for itm in kpt if itm["id"] in ids], ended_at=_now(),
                   summary=[row(itm) for itm in rpt["cycles"]])
        ports.campaign(None)
        ports.use_live(False)
        if old is not None:
            signal.signal(signal.SIGINT, old)
