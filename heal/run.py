"""Run the heal graph: python -m heal.run "prompt" --stub [--scenario retry|healed|unhealed|rejected]"""
import argparse
import sys

from heal import graph


def main(argv=None):
    arg = argparse.ArgumentParser(prog="python -m heal.run")
    arg.add_argument("prompt")
    arg.add_argument("--stub", action="store_true", help="run the stub graph (the only mode so far)")
    arg.add_argument("--scenario", default="retry", choices=graph.SCENARIOS)
    opt = arg.parse_args(argv)
    if not opt.stub:
        print("only --stub is implemented", file=sys.stderr)
        return 2
    sta, pth = graph.run(opt.prompt, opt.scenario)
    rpt = sta.get("report", {})
    print("== Path ==")
    print(" -> ".join(pth))
    print(f"== Report: outcome={rpt.get('outcome')} attempts={rpt.get('attempts')} "
          f"experiment={rpt.get('experiment')} target={rpt.get('target')} score={rpt.get('score')} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
