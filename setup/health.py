"""P1: pass/fail health table for robot-shop. TODO(P1): finish and harden."""
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

PRJ = "robot-shop"
URL = os.environ.get("ROBOT_SHOP_URL", "http://localhost:8080")


def check():
    rows = []
    try:
        import docker

        cli = docker.from_env()
        containers = cli.containers.list(
            all=True, filters={"label": f"com.docker.compose.project={PRJ}"}
        )
        if not containers:
            rows.append(("containers", False, f"no containers found for project {PRJ}"))
        for con in containers:
            service = con.labels.get("com.docker.compose.service", "unknown")
            svc = f"{service}/{con.name}"
            state = con.attrs.get("State", {}).get("Status", con.status)
            rows.append((svc, state == "running", state))
    except ImportError:
        rows.append(("Docker SDK", False, "install dependencies with: pip install -r requirements.txt"))
    except Exception as err:
        rows.append(("Docker", False, str(err)[:100]))
    try:
        with urlopen(URL, timeout=5) as response:
            status = response.status
        rows.append(("storefront", status == 200, f"HTTP {status} ({URL})"))
    except HTTPError as err:
        rows.append(("storefront", False, f"HTTP {err.code} ({URL})"))
    except (URLError, TimeoutError, ValueError) as err:
        rows.append(("storefront", False, f"{URL}: {str(err)[:80]}"))
    return rows


def main():
    rows = check()
    print(f"{'RESULT':<7} {'CHECK':<34} DETAILS")
    print(f"{'-' * 7} {'-' * 34} {'-' * 60}")
    for svc, ok, msg in sorted(rows):
        print(f"{('PASS' if ok else 'FAIL'):<7} {svc:<34} {msg}")
    sys.exit(0 if rows and all(ok for _, ok, _ in rows) else 1)


if __name__ == "__main__":
    main()
