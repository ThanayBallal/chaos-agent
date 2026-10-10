"""Run the heal graph:
python -m heal.run "prompt" --stub [--scenario retry|healed|unhealed|rejected] [--harden round1|round2|never]
python -m heal.run "prompt" --live [--baseline-s 30]   (real robot-shop; STOP file or Ctrl+C reverts everything)"""
import argparse
import json
import sys

from heal import graph


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
    for itm in rpt.get("applied_fixes", []):
        print(f"== Action attempt {itm['attempt']}: {itm['action']}({itm['target']}) ok={itm['ok']} {itm['detail']}")
    if rpt.get("outcome") == "healed":
        sec = rpt.get("healed_in_s")
        print(f"== Healed in {sec}s" if sec is not None else "== Healed (stub: not timed)")
    elif rpt.get("outcome") != "rejected":
        print(f"== Not healed: {rpt.get('outcome')}" + (f" ({rpt['abort']})" if rpt.get("abort") else ""))
    for itm in rpt.get("hardening_rounds", []):
        print(f"== Hardening round {itm['round']}: {', '.join(itm['patches']) or 'no patch'} improved={itm['improved']}")
    for cmp in rpt.get("comparisons", []):
        for res in cmp["experiments"]:
            print(f"   compare {res['id']} improved={res['improved']} error_rate={res['error_rate']} "
                  f"recovery_s={res['recovery_s']} data_survived={res['data_survived']}")
    agt = rpt.get("agents_detail") or {}
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
    try:
        opt = arg.parse_args(argv)
    except SystemExit as err:
        return err.code
    if not (opt.stub or opt.live):
        print("choose --stub or --live", file=sys.stderr)
        return 2
    if opt.live:
        from safety import guard
        guard.install()  # stale STOP removed; Ctrl+C and exit revert every registered fault
        sta, pth = graph.run(opt.prompt, live=True, baseline_s=opt.baseline_s)
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
