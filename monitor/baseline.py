"""P3: capture steady-state metrics under load."""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

URL = "http://localhost:8080"
PRJ = "robot-shop"
WRK = 4  # concurrent traffic workers
TMO = 6.0  # per-request timeout; above guard P95_MAX so a 5000 ms breach is observable
USR = {"name": "user", "password": "password"}  # robot-shop seeded user
SKU = "Watson"  # fallback sku if catalogue cannot be read
DAT = Path(__file__).resolve().parent.parent / "data"

_cli = {"docker": None}
_sku = {"val": None}


def _docker():
    if _cli["docker"] is None:
        import docker
        _cli["docker"] = docker.from_env()
    return _cli["docker"]


def health():
    """Return {svc: healthy|unhealthy|paused|starting} for every robot-shop compose container."""
    try:
        cons = _docker().containers.list(all=True, filters={"label": f"com.docker.compose.project={PRJ}"})
    except Exception:
        return {}
    out = {}
    for con in cons:
        svc = con.labels.get("com.docker.compose.service", con.name)
        sta = con.status
        chk = (con.attrs.get("State", {}).get("Health") or {}).get("Status")
        if sta == "paused":
            out[svc] = "paused"
        elif sta != "running" or chk == "unhealthy":
            out[svc] = "unhealthy"
        elif chk == "starting":
            out[svc] = "starting"
        else:
            out[svc] = "healthy"
    return out


def _pick_sku(ses):
    if _sku["val"] is None:
        try:
            rsp = ses.get(f"{URL}/api/catalogue/products", timeout=TMO)
            _sku["val"] = rsp.json()[0]["sku"]
        except Exception:
            return SKU
    return _sku["val"]


def _hit(ses, nam, wid):
    """One request. Return (endpoint, ok, ms)."""
    sta = time.monotonic()
    try:
        if nam == "catalogue":
            rsp = ses.get(f"{URL}/api/catalogue/products", timeout=TMO)
        elif nam == "cart_add":
            rsp = ses.get(f"{URL}/api/cart/add/chaos-{wid}/{_pick_sku(ses)}/1", timeout=TMO)
        else:
            rsp = ses.post(f"{URL}/api/user/login", json=USR, timeout=TMO)
        ok = 200 <= rsp.status_code < 300
    except requests.RequestException:
        ok = False
    return nam, ok, (time.monotonic() - sta) * 1000


def _drive(dur):
    """Drive catalogue / cart add / login traffic for dur seconds. Return [(endpoint, ok, ms)]."""
    end = time.monotonic() + dur
    raw, lck = [], threading.Lock()

    def loop(wid):
        ses = requests.Session()
        idx = wid
        while True:
            row = _hit(ses, ("catalogue", "cart_add", "login")[idx % 3], wid)
            with lck:
                raw.append(row)
            idx += 1
            if time.monotonic() >= end:
                break

    ths = [threading.Thread(target=loop, args=(wid,), daemon=True) for wid in range(WRK)]
    for thd in ths:
        thd.start()
    for thd in ths:
        thd.join()
    return raw


def pct(vals, p):
    """Nearest-rank percentile, rounded ms."""
    if not vals:
        return 0
    srt = sorted(vals)
    return round(srt[min(len(srt) - 1, max(0, int(round(p / 100 * len(srt))) - 1))])


def stats(raw):
    """Aggregate [(endpoint, ok, ms)] into totals plus per-endpoint numbers."""
    lat = [ms for _, _, ms in raw]
    err = sum(1 for _, ok, _ in raw if not ok)
    eps = {}
    for nam in ("catalogue", "cart_add", "login"):
        sub = [(ok, ms) for n, ok, ms in raw if n == nam]
        bad = sum(1 for ok, _ in sub if not ok)
        eps[nam] = {"requests": len(sub), "errors": bad,
                    "error_rate": round(bad / len(sub), 3) if sub else 0.0,
                    "p95_ms": pct([ms for _, ms in sub], 95)}
    return {"requests": len(raw), "errors": err,
            "error_rate": round(err / len(raw), 3) if raw else 1.0,
            "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "endpoints": eps}


def sample(dur=1.0):
    """Drive traffic for dur seconds; return {"error_rate", "p95_ms", "health": {svc: state}}.
    Also carries requests/errors/p50_ms/endpoints and "_raw" rows for aggregation by the runner."""
    raw = _drive(dur)
    return {**stats(raw), "health": health(), "_raw": raw}


def capture(win=120):
    """Return a dict matching contracts.md baseline.json and save it to data/baseline.json.
    Traffic only: never injects a fault."""
    sta = stats(_drive(win))
    bas = {"window_s": win, "requests": sta["requests"], "error_rate": sta["error_rate"],
           "p50_ms": sta["p50_ms"], "p95_ms": sta["p95_ms"], "health": health(),
           "captured_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    (DAT / "baseline.json").write_text(json.dumps(bas, indent=2))
    return bas
