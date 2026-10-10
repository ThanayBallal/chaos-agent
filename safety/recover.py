"""P5: emergency recovery. `python -m safety.recover` puts every robot-shop container back to a clean state."""
import json

from monitor.injector import DEV, NET, PID
from safety.allowlist import LBL, PRJ, SVC_LBL
from safety.guard import ACT

# only delete when a netem qdisc exists, so "no such qdisc" never happens; a failed delete still raises
NETEM = f"if tc qdisc show dev {DEV} | grep -q netem; then tc qdisc del dev {DEV} root && echo removed; fi"
CPU = f"if [ -f {PID} ]; then kill $(cat {PID}) 2>/dev/null; rm -f {PID}; echo killed; fi"


def _name(con):
    return con.labels.get(SVC_LBL, con.name)


def _image(con):
    return con.attrs.get("Config", {}).get("Image", "")


def _helpers(cli):
    """Remove leftover pumba and netshoot helper containers."""
    fix = []
    for con in cli.containers.list(all=True):
        img = _image(con)
        if "pumba" in con.name or "pumba" in img or img.split(":")[0] == NET:
            try:
                con.remove(force=True)
                fix.append(f"removed helper container {con.name} ({img})")
            except Exception as err:
                fix.append(f"FAILED removing {con.name}: {err}")
    return fix


def _netem(cli, con):
    out = cli.containers.run(NET, ["sh", "-c", NETEM], network_mode=f"container:{con.id}",
                             cap_add=["NET_ADMIN"], remove=True)
    return b"removed" in out


def recover(cli):
    """Return a list of human-readable fixes applied."""
    fix = _helpers(cli)
    try:
        cli.images.get(NET)
        hav = True
    except Exception:
        hav = False  # injector pulls it before adding netem, so no image means no netem to remove
    for con in cli.containers.list(all=True, filters={"label": f"{LBL}={PRJ}"}):
        nam = _name(con)
        try:
            if con.status == "paused":
                con.unpause()
                fix.append(f"unpaused {nam}")
            elif con.status in ("exited", "created", "dead"):
                con.start()
                fix.append(f"started {nam}")
            con.reload()
            hst = con.attrs.get("HostConfig", {})
            if (hst.get("CpuQuota") or 0) > 0:
                con.update(cpu_quota=-1)
                fix.append(f"reset cpu_quota on {nam}")
            if (hst.get("NanoCpus") or 0) > 0:
                api = cli.api
                api._raise_for_status(api._post_json(api._url("/containers/{0}/update", con.id), data={"NanoCpus": 0}))
                fix.append(f"reset cpus limit on {nam}")
        except Exception as err:
            fix.append(f"FAILED on {nam}: {err}")
            continue
        if con.status != "running":
            continue
        try:
            if b"killed" in con.exec_run(["sh", "-c", CPU]).output:
                fix.append(f"killed cpu_stress loops in {nam}")
        except Exception as err:
            fix.append(f"FAILED cpu_stress cleanup on {nam}: {err}")
        if hav:
            try:
                if _netem(cli, con):
                    fix.append(f"removed netem qdisc on {nam}")
            except Exception as err:
                fix.append(f"FAILED netem cleanup on {nam}: {err}")
    return fix


def main():
    import docker

    fix = recover(docker.from_env())
    for itm in fix:
        print(f"[recover] {itm}")
    if not fix:
        print("[recover] nothing to fix")
    if ACT.exists():
        ACT.write_text(json.dumps([]))


if __name__ == "__main__":
    main()
