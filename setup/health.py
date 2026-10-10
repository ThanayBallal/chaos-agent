"""P1: pass/fail health table for robot-shop. TODO(P1): finish and harden."""
import sys

import docker
import requests

PRJ = "robot-shop"
URL = "http://localhost:8080"


def check():
    cli = docker.from_env()
    rows = []
    for con in cli.containers.list(all=True, filters={"label": f"com.docker.compose.project={PRJ}"}):
        svc = con.labels.get("com.docker.compose.service", con.name)
        rows.append((svc, con.status == "running", con.status))
    try:
        ok = requests.get(URL, timeout=5).status_code == 200
        rows.append(("web:8080", ok, "200" if ok else "bad status"))
    except requests.RequestException as err:
        rows.append(("web:8080", False, str(err)[:40]))
    return rows


def main():
    rows = check()
    for svc, ok, msg in sorted(rows):
        print(f"{'PASS' if ok else 'FAIL'}  {svc:<12} {msg}")
    sys.exit(0 if rows and all(ok for _, ok, _ in rows) else 1)


if __name__ == "__main__":
    main()
