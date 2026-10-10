"""P5: orchestration. map -> baseline -> plan -> approve -> run -> analyze. `python main.py --mock` must always work."""
import argparse
import json
from pathlib import Path

from agent import analyst, planner
from safety import guard
from safety.allowlist import FAULTS

DAT = Path(__file__).resolve().parent / "data"
DFL = {"delay_ms": 500, "cpus": 1}  # --only defaults for the extra params of latency / cpu_stress


def load(nam):
    return json.loads((DAT / nam).read_text())


def save(nam, obj):
    (DAT / nam).write_text(json.dumps(obj, indent=2))


def only(txt):
    """'TARGET:FAULT:SECONDS' -> a single plan.json item."""
    try:
        tgt, fal, sec = txt.split(":")
        prm = {"duration_s": int(sec)}
    except ValueError:
        raise argparse.ArgumentTypeError("expected TARGET:FAULT:SECONDS, e.g. redis:kill:30")
    prm.update({key: DFL[key] for key in FAULTS.get(fal, ()) if key in DFL})
    return {"id": "e1", "target": tgt, "fault": fal, "params": prm, "risk": "manual", "rank": 1,
            "reason": "--only", "hypothesis": ""}


def main():
    arg = argparse.ArgumentParser()
    arg.add_argument("--mock", action="store_true", help="use data/mock_*.json, no Docker")
    arg.add_argument("--top", type=int, default=3)
    arg.add_argument("--only", type=only, metavar="TARGET:FAULT:SECONDS",
                     help="skip the planner and run exactly this experiment")
    opt = arg.parse_args()
    guard.install()

    if opt.mock:
        dep, bas = load("mock_depmap.json"), load("mock_baseline.json")
    else:
        from mapper.mapper import build_depmap
        from monitor import baseline
        dep, bas = build_depmap(), baseline.capture()
        save("depmap.json", dep)
        save("baseline.json", bas)

    pln = [opt.only] if opt.only else planner.plan(dep, bas, opt.top)
    save("plan.json", pln)
    print("== Plan ==")
    for exp in pln:
        print(f" {exp['rank']}. {exp['fault']:<10} {exp['target']:<10} {exp['reason']}")

    rsl = []
    if opt.mock:
        mck = {res["target"]: res for res in load("mock_results.json")}
        for exp in pln:
            rej = guard.approve(exp, dep, mock=True)
            if rej:
                print(f" [guard] rejected {exp['id']}: {rej}")
            elif exp["target"] in mck:
                rsl.append({**mck[exp["target"]], "id": exp["id"], "fault": exp["fault"]})
            else:
                print(f"[main] skipped {exp['id']}: no mock result for {exp['target']}")
    else:
        from monitor import runner
        try:
            for exp in pln:
                rej = guard.approve(exp, dep)
                if rej:
                    print(f" [guard] rejected {exp['id']}: {rej}")
                    continue
                if guard.kill_switch():
                    break
                res = runner.run(exp, bas, guard)
                if res.get("error_rate") is None:  # never injected (not steady / injection failed)
                    print(f"[main] skipped {exp['id']}: {res.get('abort_reason')}")
                    continue
                rsl.append(res)
        finally:
            guard.revert_all()
            baseline.stop()

    if not rsl:
        print("[main] no experiment results - nothing to analyze")
        return
    fnd = analyst.analyze(dep, bas, rsl, pln)
    save("findings.json", fnd)
    print(f"== Findings (resilience score {fnd['resilience_score']}/100) ==")
    for itm in fnd["findings"]:
        print(f" {itm['rank']}. [{itm['severity']}] {itm['target']}: {itm['evidence']}")


if __name__ == "__main__":
    main()
