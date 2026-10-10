"""P3: capture steady-state metrics under load.
Traffic runs in background workers, one group per endpoint, so a hanging endpoint never blocks the
1 s sampling cadence or the traffic to other endpoints."""
import itertools
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

URL = "http://localhost:8080"
PRJ = "robot-shop"
EPS = ("catalogue", "cart_add", "login")
WPE = 2  # workers per endpoint
GAP = 0.1  # min seconds between requests per worker (~10 req/s/worker)
TMO = 6.0  # per-request timeout; above guard P95_MAX so a 5000 ms wait is observable
KEEP = 300  # seconds of completed rows kept in memory
USR = {"name": "user", "password": "password"}  # robot-shop seeded user
SKU = "Watson"  # fallback sku if catalogue cannot be read
DAT = Path(__file__).resolve().parent.parent / "data"

_cli = {"docker": None}
_sku = {"val": None}
_lod = {"ths": [], "stop": None}
_lck = threading.Lock()
_done = []  # (endpoint, ok, ms, kind, t_end)
_wait = {}  # token -> (endpoint, t_start)
_tok = itertools.count()


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
    """One request. Return (ok, kind) with kind in ok | timeout | conn | http_<code>."""
    try:
        if nam == "catalogue":
            rsp = ses.get(f"{URL}/api/catalogue/products", timeout=TMO)
        elif nam == "cart_add":
            rsp = ses.get(f"{URL}/api/cart/add/chaos-{wid}/{_pick_sku(ses)}/1", timeout=TMO)
        else:
            rsp = ses.post(f"{URL}/api/user/login", json=USR, timeout=TMO)
    except requests.Timeout:
        return False, "timeout"
    except requests.RequestException:
        return False, "conn"
    ok = 200 <= rsp.status_code < 300
    return ok, "ok" if ok else f"http_{rsp.status_code}"


def _worker(nam, wid, stp):
    ses = requests.Session()
    while not stp.is_set():
        tok, sta = next(_tok), time.monotonic()
        with _lck:
            _wait[tok] = (nam, sta)
        ok, kind = _hit(ses, nam, wid)
        end = time.monotonic()
        with _lck:
            _wait.pop(tok, None)
            _done.append((nam, ok, (end - sta) * 1000, kind, end))
        stp.wait(max(0.0, GAP - (end - sta)))


def start():
    """Start background traffic if not already running."""
    if _lod["ths"] and all(thd.is_alive() for thd in _lod["ths"]):
        return
    stp = threading.Event()
    ths = [threading.Thread(target=_worker, args=(nam, f"{nam}-{num}", stp), daemon=True)
           for nam in EPS for num in range(WPE)]
    for thd in ths:
        thd.start()
    _lod.update(ths=ths, stop=stp)


def stop():
    """Stop background traffic (workers exit after their current request)."""
    if _lod["stop"]:
        _lod["stop"].set()
    _lod.update(ths=[], stop=None)


def _window(dur):
    """Wait dur seconds. Return (completed rows in the window, rows still waiting at its end)."""
    start()
    t_a = time.monotonic()
    time.sleep(dur)
    t_b = time.monotonic()
    with _lck:
        don = [row[:4] for row in _done if t_a <= row[4] < t_b]
        pen = [(nam, None, (t_b - sta) * 1000, "pending") for nam, sta in _wait.values()]
        _done[:] = [row for row in _done if row[4] >= t_b - KEEP]
    return don, pen


def pct(vals, p):
    """Nearest-rank percentile, rounded ms."""
    if not vals:
        return 0
    srt = sorted(vals)
    return round(srt[min(len(srt) - 1, max(0, int(round(p / 100 * len(srt))) - 1))])


def stats(raw):
    """Aggregate [(endpoint, ok, ms, kind)] into totals plus per-endpoint numbers.
    kind 'pending' = no response yet: counts toward latency (as a lower bound), not as an error."""
    lat = [ms for _, _, ms, _ in raw]
    err = sum(1 for _, ok, _, _ in raw if ok is False)
    eps = {}
    for nam in EPS:
        sub = [row for row in raw if row[0] == nam]
        bad = sum(1 for row in sub if row[1] is False)
        eps[nam] = {"requests": len(sub), "errors": bad,
                    "timeouts": sum(1 for row in sub if row[3] == "timeout"),
                    "pending": sum(1 for row in sub if row[3] == "pending"),
                    "error_rate": round(bad / len(sub), 3) if sub else 0.0,
                    "p95_ms": pct([row[2] for row in sub], 95)}
    return {"requests": len(raw), "errors": err,
            "error_rate": round(err / len(raw), 3) if raw else 1.0,
            "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "endpoints": eps}


def sample(dur=1.0):
    """Measure background traffic for dur seconds; return {"error_rate", "p95_ms", "health": {svc: state}}.
    Requests still waiting at the end count toward p95 with their wait so far.
    Also carries requests/errors/p50_ms/endpoints, plus "_raw" (completed rows) and "_pending" for the runner."""
    don, pen = _window(dur)
    return {**stats(don + pen), "health": health(), "_raw": don, "_pending": pen}


def capture(win=120):
    """Return a dict matching contracts.md baseline.json and save it to data/baseline.json.
    Traffic only: never injects a fault."""
    don, _ = _window(win)
    sta = stats(don)
    bas = {"window_s": win, "requests": sta["requests"], "error_rate": sta["error_rate"],
           "p50_ms": sta["p50_ms"], "p95_ms": sta["p95_ms"], "health": health(),
           "captured_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    (DAT / "baseline.json").write_text(json.dumps(bas, indent=2))
    return bas
