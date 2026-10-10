"""Run the heal graph:
python -m heal.run "prompt" --stub [--scenario retry|healed|unhealed|rejected] [--harden round1|round2|never]
python -m heal.run "prompt" --live [--baseline-s 30] [--pause-before-heal]
    (real robot-shop; STOP file or Ctrl+C reverts everything; --pause-before-heal holds the broken app after injection
    until Enter or 90 s, then heals with a fresh 60 s deadline)"""
import argparse
import json
import sys

from heal import graph


def _kept(val):
    return {True: "True (cart kept)", False: "False (cart lost)"}.get(val, "None (not probed)")


def show(rpt):
    """prompt -> experiment -> blind diagnosis (correct?) -> actions -> healed in Xs -> hardening -> LLM vs fallback."""
    exp, dxs = rpt.get("experiment_detail") or {}, rpt.get("diagnosis")
    print(f"== Prompt: {rpt.get('prompt')!r}")
    if exp:
        print(f"== Experiment: {exp['fault']} {exp['target']} {json.dumps(exp['params'])}"
              + (f"  REJECTED by guard: {exp['rejection']}" if exp.get("rejection") else ""))
        print(f"   reason: {exp['reason']}")
        print(f"   hypothesis: {exp['hypothesis']}")
    if dxs:
        scr = rpt.get("diagnosis_score") or {}
        print(f"== Diagnosis (blind): suspect={dxs['suspect_service']} fault={dxs['fault_guess']} "
              f"confidence={dxs['confidence']} -> {'CORRECT' if scr.get('correct') else 'WRONG'} "
              f"(service {'ok' if scr.get('service') else 'wrong'}, fault {'ok' if scr.get('fault') else 'wrong'})")
        for itm in dxs["evidence"]:
            print(f"   evidence: {itm}")
    if rpt.get("held_s") is not None:
        print(f"== Held for {rpt['held_s']}s before healing ({rpt.get('held_by')})")
    for itm in rpt.get("applied_fixes", []):
        print(f"== Action attempt {itm['attempt']}: {itm['action']}({itm['target']}) ok={itm['ok']} {itm['detail']}")
    if rpt.get("outcome") == "healed" or (rpt.get("outcome") == "stopped_by_kill_switch" and rpt.get("healed")):
        sec = rpt.get("healed_in_s")
        print(f"== Healed in {sec}s" if sec is not None else "== Healed (stub: not timed)")
    elif rpt.get("outcome") not in ("rejected", "stopped_by_kill_switch"):
        print(f"== Not healed: {rpt.get('outcome')}" + (f" ({rpt['abort']})" if rpt.get("abort") else ""))
    if rpt.get("hardening") == "not_applicable":
        print(f"== Hardening: not applicable ({rpt.get('hardening_note')})")
    for itm in rpt.get("hardening_rounds", []):
        print(f"== Hardening round {itm['round']}: {', '.join(itm['patches']) or 'no patch'} improved={itm['improved']}"
              + (f" ({itm['why']})" if itm.get("why") else ""))
        if itm.get("score"):
            print(f"   before -> after: error_rate={itm['error_rate']} recovery_s={itm['recovery_s']} "
                  f"data_survived={itm['data_survived']} score={itm['score']['before']} -> {itm['score']['after']}")
    if rpt.get("hardening") == "not_improved" and rpt.get("hardening_note"):
        print(f"== Hardening: {rpt['hardening_note']}")
    dsv = rpt.get("data_survived") or {}
    if rpt.get("rounds"):
        print(f"== Data survived (cart probe): before={_kept(dsv.get('before'))}"
              + "".join(f", rerun {idx}={_kept(val)}" for idx, val in enumerate(dsv.get("rerun") or [], 1)))
    if rpt.get("outcome") == "stopped_by_kill_switch":
        print(f"== Stopped by kill switch during hardening round {rpt.get('rounds')}: {rpt.get('stopped')}; "
              f"no further hardening fix applied")
    agt =rpt.get("agents_detail") or {}
    print("== Agents: " + ", ".join(
        f"{nam}={'+'.join(met['source'] for met in mets)}"
        + "".join(f" ({why})" for why in dict.fromkeys(met["why"] for met in mets if met.get("why")))
        for nam, mets in agt.items()))


def safe_output():
    """LLM text can hold characters the console cannot encode (e.g. an arrow on a cp1252 redirect): print them as '?'
    instead of raising mid-run. ASCII output is unchanged."""
    for stm in (sys.stdout, sys.stderr):
        try:
            stm.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    safe_output()
    arg = argparse.ArgumentParser(prog="python -m heal.run")
    arg.add_argument("prompt")
    mod = arg.add_mutually_exclusive_group()
    mod.add_argument("--stub", action="store_true", help="run the stub graph (fake data, no Docker)")
    mod.add_argument("--live", action="store_true", help="run against the live robot-shop compose project")
    arg.add_argument("--scenario", default="retry", choices=graph.SCENARIOS, help="stub heal outcome")
    arg.add_argument("--harden", default="round1", choices=graph.HARDEN, help="stub: which hardening round improves")
    arg.add_argument("--baseline-s", type=int, default=30, help="live: seconds of baseline traffic to capture")
    arg.add_argument("--pause-before-heal", action="store_true",
                     help="live: after injection wait for Enter (or 90 s) before healing, kill switch armed")
    try:
        opt = arg.parse_args(argv)
    except SystemExit as err:
        return err.code
    if not (opt.stub or opt.live):
        print("choose --stub or --live", file=sys.stderr)
        return 2
    if opt.pause_before_heal and not opt.live:
        print("--pause-before-heal needs --live", file=sys.stderr)
        return 2
    if opt.live:
        from safety import guard
        guard.install()  # stale STOP removed; Ctrl+C and exit revert every registered fault
        sta, pth = graph.run(opt.prompt, live=True, baseline_s=opt.baseline_s, hold=opt.pause_before_heal)
    else:
        sta, pth = graph.run(opt.prompt, opt.scenario, harden=opt.harden)
    rpt = sta.get("report", {})
    print("== Path ==")
    print(" -> ".join(pth))
    print(f"== Report: outcome={rpt.get('outcome')} hardening={rpt.get('hardening')} attempts={rpt.get('attempts')} "
          f"rounds={rpt.get('rounds')} experiment={rpt.get('experiment')} target={rpt.get('target')} "
          f"score={rpt.get('score')} ==")
    if rpt.get("abort"):
        print(f"== Aborted: {rpt['abort']} ==")
    show(rpt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
