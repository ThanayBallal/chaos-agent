"""P5: emergency recovery. `python -m safety.recover` puts every robot-shop container back to a clean state."""
import json

from safety.allowlist import LBL, PRJ, SVC_LBL
from safety.guard import ACT


def _name(con):
    return con.labels.get(SVC_LBL, con.name)


def recover(cli):
    """Return a list of human-readable fixes applied."""
    fix = []
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
    for con in cli.containers.list(all=True):
        img = con.attrs.get("Config", {}).get("Image", "")
        if "pumba" in con.name or "pumba" in img:
            try:
                con.remove(force=True)
                fix.append(f"removed pumba container {con.name}")
            except Exception as err:
                fix.append(f"FAILED removing {con.name}: {err}")
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
