"""Run the heal graph:
python -m heal.run "prompt" --stub [--scenario retry|healed|unhealed|rejected] [--harden round1|round2|never]
python -m heal.run "prompt" --live [--baseline-s 30]   (real robot-shop; STOP file or Ctrl+C reverts everything)"""
import argparse
import sys

from heal import graph


def main(argv=None):
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
    if opt.live:
        for itm in rpt.get("applied_fixes", []):
            print(f" fix attempt {itm['attempt']}: {itm['action']}({itm['target']}) ok={itm['ok']} {itm['detail']}")
        for cmp in rpt.get("comparisons", []):
            for exp in cmp["experiments"]:
                print(f" compare {exp['id']} improved={exp['improved']} error_rate={exp['error_rate']} "
                      f"recovery_s={exp['recovery_s']} data_survived={exp['data_survived']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
