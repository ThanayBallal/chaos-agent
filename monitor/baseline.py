"""P3: capture steady-state metrics under load.
Traffic is fixed-rate (open loop): each endpoint gets RATE requests per second on schedule, whether or not
earlier ones have answered, like real users. A hanging endpoint therefore shows its true failure share, and
never blocks the 1 s sampling cadence or the traffic to other endpoints."""
import itertools
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

URL = "http://localhost:8080"
PRJ = "robot-shop"
EPS = ("catalogue", "cart_add", "login")
RATE = 10  # requests per second per endpoint
TMO = 6.0  # per-request timeout; above guard P95_MAX so a 5000 ms wait is observable
CAP = int(2 * RATE * TMO)  # max in flight per endpoint (2x what RATE x TMO needs); beyond it requests are dropped, not sent
KEEP = 300  # seconds of completed rows kept in memory
USR = {"name": "user", "password": "password"}  # robot-shop seeded user
SKU = "Watson"  # fallback sku if catalogue cannot be read
DAT = Path(__file__).resolve().parent.parent / "data"

_cli = {"docker": None}
_sku = {"val": None}
_lod = {"ths": [], "stop": None, "pools": []}
_lck = threading.Lock()
_done = []  # (endpoint, ok, ms, kind, t_end, t_start)
_wait = {}  # token -> (endpoint, t_start)
_tok = itertools.count()
_drop = {nam: 0 for nam in EPS}  # requests not sent because CAP was reached
_fly = {nam: 0 for nam in EPS}
_ses = threading.local()


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


def _one(nam):
    """Send one request on a pool thread and record it."""
    if not hasattr(_ses, "s"):
        _ses.s = requests.Session()
    tok, sta = next(_tok), time.monotonic()
    ok, kind = False, "client_error"
    with _lck:
        _wait[tok] = (nam, sta)
    try:
        ok, kind = _hit(_ses.s, nam, threading.get_ident() % 8)
    finally:
        end = time.monotonic()
        with _lck:
            _wait.pop(tok, None)
            _fly[nam] -= 1
    with _lck:
        _done.append((nam, ok, (end - sta) * 1000, kind, end, sta))


def _schedule(nam, pool, stp):
    """Submit RATE requests per second for endpoint nam until stopped."""
    nxt = time.monotonic()
    while not stp.is_set():
        with _lck:
            full = _fly[nam] >= CAP
            if not full:
                _fly[nam] += 1
        if full:
            with _lck:
                _drop[nam] += 1
        else:
            pool.submit(_one, nam)
        nxt += 1 / RATE
        stp.wait(max(0.0, nxt - time.monotonic()))


def start():
    """Start background traffic if not already running."""
    if _lod["ths"] and all(thd.is_alive() for thd in _lod["ths"]):
        return
    stp = threading.Event()
    pools = [ThreadPoolExecutor(max_workers=CAP, thread_name_prefix=f"load-{nam}") for nam in EPS]
    ths = [threading.Thread(target=_schedule, args=(nam, pool, stp), daemon=True) for nam, pool in zip(EPS, pools)]
    for thd in ths:
        thd.start()
    _lod.update(ths=ths, stop=stp, pools=pools)


def stop():
    """Stop background traffic. Requests already sent finish (or time out) on their own."""
    if _lod["stop"]:
        _lod["stop"].set()
    for pool in _lod["pools"]:
        pool.shutdown(wait=False, cancel_futures=True)
    _lod.update(ths=[], stop=None, pools=[])


def _window(dur):
    """Wait dur seconds. Return (completed rows in the window, rows still waiting at its end)."""
    start()
    t_a = time.monotonic()
    time.sleep(dur)
    t_b = time.monotonic()
    with _lck:
        don = [row[:4] for row in _done if t_a <= row[4] < t_b]
        don += [(nam, None, 0, "dropped") for nam in EPS for _ in range(_drop[nam])]
        _drop.update({nam: 0 for nam in EPS})
        pen = [(nam, None, (t_b - sta) * 1000, "pending") for nam, sta in _wait.values()]
        _done[:] = [row for row in _done if row[4] >= t_b - KEEP]
    return don, pen


def sent_between(t_a, t_b):
    """Rows for requests SENT in [t_a, t_b) (monotonic), each with its final outcome if it has one yet."""
    now = time.monotonic()
    with _lck:
        don = [row[:4] for row in _done if t_a <= row[5] < t_b]
        pen = [(nam, None, (now - sta) * 1000, "pending") for nam, sta in _wait.values() if t_a <= sta < t_b]
    return don + pen


def pct(vals, p):
    """Nearest-rank percentile, rounded ms."""
    if not vals:
        return 0
    srt = sorted(vals)
    return round(srt[min(len(srt) - 1, max(0, int(round(p / 100 * len(srt))) - 1))])


def stats(raw):
    """Aggregate [(endpoint, ok, ms, kind)] into totals plus per-endpoint numbers.
    kind 'pending' = no response yet: counts toward latency (as a lower bound), not as an error.
    kind 'dropped' = not sent because our own in-flight cap was hit: excluded from all rates, only counted."""
    drp = [row for row in raw if row[3] == "dropped"]
    raw = [row for row in raw if row[3] != "dropped"]
    lat = [ms for _, _, ms, _ in raw]
    err = sum(1 for _, ok, _, _ in raw if ok is False)
    eps = {}
    for nam in EPS:
        sub = [row for row in raw if row[0] == nam]
        bad = sum(1 for row in sub if row[1] is False)
        eps[nam] = {"requests": len(sub), "errors": bad,
                    "timeouts": sum(1 for row in sub if row[3] == "timeout"),
                    "pending": sum(1 for row in sub if row[3] == "pending"),
                    "dropped": sum(1 for row in drp if row[0] == nam),
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
