"""P3: live check that every fault is applied AND fully reverted on a real robot-shop container.
Run: python -m monitor.live_check [service] [faults...]   e.g. python -m monitor.live_check redis kill pause cpu_stress
Each fault runs for HOLD seconds; revert always runs (finally), so Ctrl+C leaves nothing behind."""
import sys
import time

from monitor import injector

HOLD = 3
CPU_BUSY = 50.0  # % of one core: fault counts as applied above this
CPU_IDLE = 20.0  # % of one core: CPU counts as restored below this
PRM = {"kill": {"duration_s": HOLD}, "pause": {"duration_s": HOLD},
       "latency": {"duration_s": HOLD, "delay_ms": 300}, "cpu_stress": {"duration_s": HOLD, "cpus": 1}}


def cpu_pct(con):
    """CPU use of the container in % of one core, from one docker stats sample."""
    sta = con.stats(stream=False)
    cpu, pre = sta["cpu_stats"], sta.get("precpu_stats", {})
    dlt = cpu["cpu_usage"]["total_usage"] - pre.get("cpu_usage", {}).get("total_usage", 0)
    sysd = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
    return round(dlt / sysd * ncpu * 100, 1) if sysd > 0 else 0.0


def netem(con):
    return b"netem" in injector._tc(con, f"tc qdisc show dev {injector.DEV}")


def state(con, fal):
    """What we measure for each fault."""
    con.reload()
    if fal in ("kill", "pause"):
        return con.status
    if fal == "cpu_stress":
        return cpu_pct(con)
    return "netem" if netem(con) else "no netem"


def applied(fal, val):
    return {"kill": val == "exited", "pause": val == "paused",
            "cpu_stress": isinstance(val, float) and val >= CPU_BUSY, "latency": val == "netem"}[fal]


def restored(fal, val):
    return {"kill": val == "running", "pause": val == "running",
            "cpu_stress": isinstance(val, float) and val <= CPU_IDLE, "latency": val == "no netem"}[fal]


def check(svc, fal):
    con = injector.container(svc)
    bef = state(con, fal)
    rev = injector.inject({"target": svc, "fault": fal, "params": PRM[fal]})
    try:
        time.sleep(1)
        dur = state(con, fal)
        time.sleep(max(0, HOLD - 1))
    finally:
        rev()
    time.sleep(2 if fal == "cpu_stress" else 0.5)
    aft = state(con, fal)
    ok = applied(fal, dur) and restored(fal, aft)
    print(f"{'PASS' if ok else 'FAIL'}  {fal:<10} {svc:<10} before={bef}  during={dur}  after={aft}  [{rev.info}]")
    return ok


def main():
    arg = sys.argv[1:]
    svc = arg[0] if arg else "redis"
    fls = arg[1:] or ["kill", "pause", "cpu_stress", "latency"]
    res = []
    for fal in fls:
        try:
            res.append(check(svc, fal))
        except Exception as err:
            print(f"FAIL  {fal:<10} {svc:<10} {type(err).__name__}: {err}")
            res.append(False)
    sys.exit(0 if all(res) else 1)


if __name__ == "__main__":
    main()
