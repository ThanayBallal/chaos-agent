"""P5: orchestration. map -> baseline -> plan -> approve -> run -> analyze. `python main.py --mock` must always work."""
import argparse
import json
from pathlib import Path

from agent import analyst, planner
from safety import guard

DAT = Path(__file__).resolve().parent / "data"


def load(nam):
    return json.loads((DAT / nam).read_text())


def save(nam, obj):
    (DAT / nam).write_text(json.dumps(obj, indent=2))


def main():
    arg = argparse.ArgumentParser()
    arg.add_argument("--mock", action="store_true", help="use data/mock_*.json, no Docker")
    arg.add_argument("--top", type=int, default=3)
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

    pln = planner.plan(dep, bas, opt.top)
    save("plan.json", pln)
    print("== Plan ==")
    for exp in pln:
        print(f" {exp['rank']}. {exp['fault']:<10} {exp['target']:<10} {exp['reason']}")

    if opt.mock:
        mck = {res["target"]: res for res in load("mock_results.json")}
        rsl = []
        for exp in pln:
            rej = guard.approve(exp, dep)
            if rej:
                print(f" [guard] rejected {exp['id']}: {rej}")
            elif exp["target"] in mck:
                rsl.append({**mck[exp["target"]], "id": exp["id"], "fault": exp["fault"]})
    else:
        from monitor import runner
        rsl = []
        for exp in pln:
            rej = guard.approve(exp, dep)
            if rej:
                print(f" [guard] rejected {exp['id']}: {rej}")
                continue
            if guard.kill_switch():
                break
            rsl.append(runner.run(exp, bas, guard))
        guard.revert_all()

    fnd = analyst.analyze(dep, bas, rsl, pln)
    save("findings.json", fnd)
    print(f"== Findings (resilience score {fnd['resilience_score']}/100) ==")
    for itm in fnd["findings"]:
        print(f" {itm['rank']}. [{itm['severity']}] {itm['target']}: {itm['evidence']}")


if __name__ == "__main__":
    main()
