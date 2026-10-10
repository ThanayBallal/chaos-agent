"""P5: live guard self-test. `python -m safety.selftest` pauses ratings until STOP / Ctrl+C / 60s, then reverts."""
import json
import signal
import sys
import time

from safety import guard
from safety.allowlist import LBL, MAX_DUR, PRJ, SVC_LBL

DAT = guard.ROT / "data"
TGT = "ratings"


def load(nam, fbk):
    pth = DAT / nam
    return json.loads((pth if pth.exists() else DAT / fbk).read_text())


def health(cli):
    """{service: healthy|unhealthy} for every robot-shop container, from real docker state."""
    out = {}
    for con in cli.containers.list(all=True, filters={"label": f"{LBL}={PRJ}"}):
        hck = con.attrs.get("State", {}).get("Health", {}).get("Status")
        ok = con.status == "running" and hck in (None, "healthy")
        out[con.labels.get(SVC_LBL, con.name)] = "healthy" if ok else "unhealthy"
    return out


def main():
    import docker

    cli = docker.from_env()
    guard.install()
    signal.signal(signal.SIGINT, signal.default_int_handler)  # Ctrl+C -> KeyboardInterrupt -> our finally
    dep, bas = load("depmap.json", "mock_depmap.json"), load("baseline.json", "mock_baseline.json")

    exp = {"id": "selftest", "target": TGT, "fault": "pause", "params": {"duration_s": MAX_DUR}}
    rej = guard.approve(exp, dep)
    if rej:
        print(f"[selftest] rejected: {rej}")
        return 1
    guard.reset()
    rsn = guard.ready({"t": 0, "error_rate": 0.0, "p95_ms": 0, "health": health(cli)}, bas)
    if rsn:
        print(f"[selftest] {rsn} - nothing paused")
        return 1

    con = cli.containers.list(all=True, filters={"label": [f"{LBL}={PRJ}", f"{SVC_LBL}={TGT}"]})[0]

    def rev():
        con.reload()
        if con.status == "paused":
            con.unpause()

    guard.register(rev, exp)
    try:
        con.pause()
        print(f"[selftest] paused {TGT}; blast radius {sorted(guard._ctx['blast'] or {TGT})}")
        print(f"[selftest] create {guard.STP.name} in repo root or press Ctrl+C to revert (max {MAX_DUR}s)")
        t0 = time.monotonic()
        while True:
            elp = time.monotonic() - t0
            if elp >= MAX_DUR:
                print("[selftest] max duration reached")
                break
            hlt = health(cli)
            rsn = guard.check({"t": elp, "error_rate": 0.0, "p95_ms": 0, "health": hlt}, bas, TGT)
            con.reload()
            bad = sorted(svc for svc, sta in hlt.items() if sta == "unhealthy")
            print(f"[selftest] t={elp:3.0f}s {TGT}={con.status} unhealthy={bad} check={rsn or 'ok'}")
            if rsn:
                print(f"[selftest] abort: {rsn}")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[selftest] Ctrl+C")
    finally:
        rev()
        guard.unregister(rev)

    con.reload()
    if guard.kill_switch():
        print(f"[selftest] note: {guard.STP.name} left in place; next guard.install() clears it")
    if con.status == "running":
        print(f"[selftest] OK - {TGT} is running again")
        return 0
    print(f"[selftest] WARNING - {TGT} status is '{con.status}'; run python -m safety.recover")
    return 1


if __name__ == "__main__":
    sys.exit(main())
