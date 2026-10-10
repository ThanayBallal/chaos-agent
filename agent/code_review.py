"""P4: source-code review of the system under test -> code-grounded chaos experiments.

The agent reads robot-shop's source (how each service connects to its dependencies), asks the LLM to find
resilience weaknesses with exact file:line citations, verifies every citation against the real file, and turns
the confirmed weaknesses into experiments that target the dependency the weak code relies on.
No key / no quota -> a static rule scan over the same files produces the same shape, so the demo never stalls.
A successful LLM review is cached by code hash in data/code_review.json, so re-runs cost no quota.
"""
import hashlib
import json
import os
import re
from pathlib import Path

import yaml

from agent import llm
from safety import guard
from safety.allowlist import FAULTS, MAX_DUR, P95_MAX

ROT = Path(__file__).resolve().parent.parent
APP = Path(os.environ.get("ROBOT_SHOP_DIR") or ROT.parent / "robot-shop")
OUT = ROT / "data" / "code_review.json"
EXT = {".js", ".py", ".go", ".java", ".php"}
SKP = {"node_modules", "vendor", "test", "tests", "load-gen", "public", "static", "var"}
KEY = re.compile(r"connect|retry|reconnect|timeout|settimeout|\.on\(|catch|except|health|ready|getenv|process\.env"
                 r"|createclient|dial|blockingconnection|pdo|jdbc|setex|publish|requests\.|http", re.I)
CTX, PER, MAXC = 2, 60, 12000
CAP = float(os.environ.get("CHAOS_REVIEW_CAP", 60))
DUR = {"kill": 30, "pause": 20, "latency": 30, "cpu_stress": 30}
HOST = {"redis": "redis", "mongo": "mongodb", "rabbit": "rabbitmq", "amqp": "rabbitmq", "mysql": "mysql"}
LAST = {}

SYS = f"""You are the code-review stage of a chaos-engineering agent for the Docker Compose app "robot-shop".
You get numbered source excerpts ("path:line| code") showing how each service talks to its dependencies,
plus the dependency map. Find resilience weaknesses in that code, e.g. missing connect/request timeouts,
retries only on first connect, health flags that are set true but never reset, errors that are only logged,
state kept only in an in-memory store, or side effects done before a call that can still fail.

For each weakness return:
- service: the service whose code is weak; dependency: the service it depends on (both must be in the dependency map)
- file and line: copied from an excerpt prefix; quote: the code on that line copied verbatim (at least 8 characters)
- weakness: one sentence describing what the code does wrong
- fault: the fault to inject into the DEPENDENCY that would expose it, one of {sorted(FAULTS)};
  params with exactly these integer keys: {json.dumps({fal: sorted(prm) for fal, prm in FAULTS.items()})};
  duration_s 1..{MAX_DUR}, latency delay_ms below {P95_MAX}
- hypothesis: a falsifiable prediction of what users and the health checks will show during and after the fault
- fix: a concrete code or compose change in that file
Only report what the excerpts show. Never invent files, lines or code. 3 to 8 weaknesses, most severe first."""


def _files(svc):
    """Source files of one service, relative to APP."""
    bas = APP / svc
    if not bas.is_dir():
        return []
    out = []
    for pth in sorted(bas.rglob("*")):
        if pth.suffix in EXT and pth.is_file() and not SKP & set(pth.relative_to(APP).parts):
            out.append(pth.relative_to(APP).as_posix())
    return out


def _read(rel):
    try:
        return (APP / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def collect(dep):
    """Return (pack {svc: [excerpt lines]}, src {rel: [lines]}) for every app service with source on disk."""
    dps = {svc["name"] for svc in dep["services"]}
    hit = re.compile(KEY.pattern + "|" + "|".join(map(re.escape, sorted(dps | set(HOST)))), re.I)
    pack, src = {}, {}
    for svc in dep["services"]:
        if svc.get("kind") not in ("app", "proxy"):
            continue
        out = []
        for rel in _files(svc["name"]):
            lns = _read(rel)
            if lns is None:
                continue
            src[rel] = lns
            keep = sorted({idx for num, lin in enumerate(lns) if hit.search(lin)
                           for idx in range(max(0, num - CTX), min(len(lns), num + CTX + 1))})
            prv = None
            for idx in keep:
                if prv is not None and idx != prv + 1:
                    out.append("...")
                out.append(f"{rel}:{idx + 1}| {lns[idx].rstrip()[:160]}")
                prv = idx
        if out:
            pack[svc["name"]] = out[:PER]
    tot = 0
    for nam in list(pack):
        tot += sum(len(lin) for lin in pack[nam])
        if tot > MAXC:
            pack[nam] = pack[nam][: max(10, PER // 3)]
    return pack, src


def _dep_of(txt, dps):
    low = txt.lower()
    for key, nam in HOST.items():
        if key in low and nam in dps:
            return nam
    return next((nam for nam in sorted(dps) if re.search(rf"\b{re.escape(nam)}\b", low)), None)


def _svc(rel):
    return rel.split("/", 1)[0]


def _risk(rel, num, lns, dpn, wek, fal, hyp, fix, wgt):
    return {"service": _svc(rel), "dependency": dpn, "file": rel, "line": num,
            "quote": lns[num - 1].strip()[:160], "weakness": wek, "fault": fal,
            "params": _params(fal), "hypothesis": hyp, "fix": fix, "weight": wgt}


def _params(fal, src=None):
    src = src or {}
    out = {"duration_s": src.get("duration_s") or DUR[fal]}
    if fal == "latency":
        out["delay_ms"] = src.get("delay_ms") or 1500
    if fal == "cpu_stress":
        out["cpus"] = src.get("cpus") or 1
    return out


def _compose():
    try:
        return (yaml.safe_load((APP / "docker-compose.yaml").read_text()) or {}).get("services", {})
    except (OSError, yaml.YAMLError):
        return {}


def static_scan(dep, src):
    """Rule-based weaknesses straight from the source text. Every risk cites a real file:line."""
    dps = {svc["name"] for svc in dep["services"]}
    cmp = _compose()
    out = []
    for rel, lns in src.items():
        txt = "\n".join(lns)
        for num, lin in enumerate(lns, 1):
            flg = re.search(r"\b(\w+Connected)\s*=\s*true\b", lin)
            if flg and not re.search(rf"^(?!\s*(?:var|let|const)\b).*\b{flg.group(1)}\s*=\s*false\b", txt, re.M):
                dpn = _dep_of(flg.group(1), dps)
                if dpn:
                    out.append(_risk(rel, num, lns, dpn,
                                     f"{flg.group(1)} is set true once and never reset, so /health keeps reporting "
                                     f"{dpn} as connected after it is lost", "kill",
                                     f"while {dpn} is down, {_svc(rel)} requests fail but its health flag still "
                                     f"says {dpn} is connected; errors stop once {dpn} is back",
                                     f"set {flg.group(1)} = false in the client's error/close handlers and make "
                                     f"/health return 503 when it is false", 3))
            if re.search(r"setTimeout\(\s*\w*[Ll]oop", lin) and re.search(r"\.catch\(|catch\s*\(", "\n".join(lns[max(0, num - 4):num])):
                dpn = _dep_of("\n".join(lns[max(0, num - 25):num]), dps)
                if dpn:
                    out.append(_risk(rel, num, lns, dpn,
                                     f"the {dpn} retry loop only covers the first connect; later disconnects are "
                                     f"left to the driver with no app-level handling", "pause",
                                     f"{_svc(rel)} requests hang or fail while {dpn} is frozen and the service "
                                     f"may need longer than the fault to recover", "handle disconnect/close events: "
                                     f"mark the dependency down, retry with backoff, and fail fast with a timeout", 2))
            if re.search(r"\.on\(\s*['\"]error['\"]", lin) and num < len(lns) and \
                    re.search(r"log", lns[num]) and re.search(r"^\s*\}\);?\s*$", lns[num + 1] if num + 1 < len(lns) else ""):
                dpn = _dep_of(lin + lns[max(0, num - 8)], dps) or _dep_of(txt[:2000], dps)
                if dpn:
                    out.append(_risk(rel, num, lns, dpn, f"the {dpn} client error handler only logs; callers get "
                                     f"no fallback or fast failure", "kill",
                                     f"{_svc(rel)} calls that need {dpn} error out for as long as it is down",
                                     "on error mark the dependency unavailable and return a fast 503 or a cached "
                                     "fallback; add reconnect backoff", 1))
            if re.search(r"\bsetex\(", lin):
                dpn = _dep_of(lin, dps) or ("redis" if "redis" in dps and "redis" in txt.lower() else None)
                svc = cmp.get(dpn or "", {})
                if dpn and not svc.get("volumes"):
                    out.append(_risk(rel, num, lns, dpn, f"{_svc(rel)} keeps its only copy of user state in {dpn}, "
                                     f"which has no volume or persistence in docker-compose.yaml", "kill",
                                     f"after {dpn} is restarted, existing {_svc(rel)} data (e.g. carts) is gone even "
                                     f"though the service reports healthy", f"give {dpn} a volume with AOF "
                                     f"persistence (appendonly yes) or a replica; treat it as a cache, not the store", 3))
            req = re.search(r"requests\.(get|post|put|delete)\(", lin)
            if req and "timeout" not in lin:
                dpn = _dep_of(lin, dps)
                if dpn and dpn != _svc(rel):
                    out.append(_risk(rel, num, lns, dpn, f"HTTP call to {dpn} has no timeout, so a slow {dpn} "
                                     f"blocks the worker indefinitely", "pause",
                                     f"while {dpn} is frozen, {_svc(rel)} requests hang instead of failing fast",
                                     "pass timeout=(connect, read) to every requests call and retry with backoff", 2))
            if "ConnectionParameters(" in lin:
                blk = "\n".join(lns[num - 1:num + 6])
                dpn = _dep_of(txt, dps)
                if dpn and not re.search(r"socket_timeout|blocked_connection_timeout|heartbeat", blk):
                    out.append(_risk(rel, num, lns, dpn, f"the {dpn} connection is created without socket or "
                                     f"blocked-connection timeouts", "pause",
                                     f"while {dpn} is frozen, publishing blocks and {_svc(rel)} requests hang",
                                     "set socket_timeout and blocked_connection_timeout on ConnectionParameters "
                                     "and fail the request fast", 2))
    see, uni = set(), []
    for rsk in out:
        tag = (rsk["file"], rsk["line"], rsk["dependency"])
        if tag not in see:
            see.add(tag)
            uni.append(rsk)
    return uni


def _schema(dep):
    num, txt = {"type": "integer"}, {"type": "string"}
    nam = sorted(svc["name"] for svc in dep["services"])
    itm = {"type": "object", "additionalProperties": False,
           "required": ["service", "dependency", "file", "line", "quote", "weakness", "fault", "params",
                        "hypothesis", "fix"],
           "properties": {"service": {"type": "string", "enum": nam}, "dependency": {"type": "string", "enum": nam},
                          "file": txt, "line": num, "quote": txt, "weakness": txt,
                          "fault": {"type": "string", "enum": sorted(FAULTS)},
                          "params": {"type": "object", "additionalProperties": False, "required": ["duration_s"],
                                     "properties": {key: num for key in sorted(set().union(*FAULTS.values()))}},
                          "hypothesis": txt, "fix": txt}}
    return {"type": "object", "additionalProperties": False, "required": ["risks"],
            "properties": {"risks": {"type": "array", "items": itm}}}


def _squash(txt):
    return re.sub(r"\s+", "", txt)


def verify(raw, dep, src):
    """Keep only LLM risks whose citation is real; raise ValueError if none survive."""
    rsk = raw.get("risks") if isinstance(raw, dict) else None
    if not isinstance(rsk, list) or not rsk:
        raise ValueError("'risks' must be a non-empty list")
    dps = {svc["name"] for svc in dep["services"]}
    out, why = [], []
    for itm in rsk:
        if not isinstance(itm, dict):
            continue
        rel, num, quo = itm.get("file"), itm.get("line"), _squash(str(itm.get("quote", "")))
        lns = src.get(rel)
        if lns is None or not isinstance(num, int) or not 1 <= num <= len(lns) or len(quo) < 8:
            why.append(f"{rel}:{num} not a real file/line")
            continue
        if not any(quo in _squash(lns[idx]) or _squash(lns[idx]) in quo and len(_squash(lns[idx])) >= 8
                   for idx in range(max(0, num - 3), min(len(lns), num + 2))):
            why.append(f"{rel}:{num} quote not found in the file")
            continue
        if itm.get("dependency") not in dps or itm.get("fault") not in FAULTS:
            why.append(f"{rel}:{num} bad dependency/fault")
            continue
        prm = _params(itm["fault"], itm.get("params") or {})
        if not 0 < prm["duration_s"] <= MAX_DUR or prm.get("delay_ms", 1) >= P95_MAX:
            prm = _params(itm["fault"])
        out.append({"service": itm.get("service") or _svc(rel), "dependency": itm["dependency"], "file": rel,
                    "line": num, "quote": lns[num - 1].strip()[:160], "weakness": str(itm.get("weakness", "")).strip(),
                    "fault": itm["fault"], "params": prm, "hypothesis": str(itm.get("hypothesis", "")).strip(),
                    "fix": str(itm.get("fix", "")).strip(), "weight": 3})
    if not out:
        raise ValueError(f"no citation could be verified ({'; '.join(why[:4])}); quote real lines from the excerpts")
    return out


def _hash(pack, dep):
    mdl = os.environ.get("LLM_MODEL", "")
    return hashlib.sha256(json.dumps([pack, sorted(svc["name"] for svc in dep["services"]), mdl]).encode()).hexdigest()[:16]


def _cached(sig):
    try:
        old = json.loads(OUT.read_text())
    except (OSError, ValueError):
        return None
    if old.get("source") == "llm" and old.get("hash") == sig and old.get("risks") and not os.environ.get("CHAOS_REVIEW_FRESH"):
        return old
    return None


def review(dep):
    """Read the source and return {"source", "files_read", "risks", ...}; saved to data/code_review.json."""
    pack, src = collect(dep)
    if not src:
        raise FileNotFoundError(f"no robot-shop source under {APP} (set ROBOT_SHOP_DIR)")
    sig = _hash(pack, dep)
    hit = _cached(sig)
    if hit:
        print(f"[code-review] reusing cached LLM review of {hit['files_read']} files ({len(hit['risks'])} risks)")
        return hit
    rev = {"hash": sig, "files_read": len(src), "services": sorted(pack)}
    usr = (f"Dependency map edges: {json.dumps(dep['edges'])}\n\nSource excerpts per service:\n\n"
           + "\n\n".join(f"### {nam}\n" + "\n".join(lns) for nam, lns in pack.items())
           + "\n\nReturn {\"risks\": [...]}.")
    try:
        rsk = llm.ask_json(SYS, usr, schema=_schema(dep), check=lambda raw: verify(raw, dep, src), cap=CAP)
        rev.update(source="llm", risks=rsk)
        print(f"[code-review] LLM read {len(src)} source files and found {len(rsk)} verified weaknesses")
    except Exception as err:
        rsk = static_scan(dep, src)
        rev.update(source="static", risks=rsk, llm_error=str(err)[:200])
        print(f"[code-review] LLM review unavailable ({str(err)[:120]}); static scan of {len(src)} files "
              f"found {len(rsk)} weaknesses")
    try:
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps(rev, indent=2))
    except OSError:
        pass
    return rev


def to_plan(rev, dep, bas, cen, top=3):
    """Group verified risks by (dependency, fault) into plan.json items, ranked by weight x centrality."""
    hea = bas.get("health", {})
    grp = {}
    for rsk in rev.get("risks", []):
        if hea.get(rsk["dependency"], "healthy") != "healthy":
            continue
        grp.setdefault((rsk["dependency"], rsk["fault"]), []).append(rsk)
    rnk = sorted(grp.items(), key=lambda kv: -sum(rsk["weight"] for rsk in kv[1]) * (1 + cen.get(kv[0][0], 0)))
    pln, see = [], set()
    LAST.clear()
    for (dpn, fal), rsk in rnk:
        if dpn in see:
            continue
        rsk = sorted(rsk, key=lambda itm: -itm["weight"])
        idx = len(pln) + 1
        cit = "; ".join(f"{itm['file']}:{itm['line']} {itm['weakness']}" for itm in rsk[:2])
        itm = {"id": f"e{idx}", "target": dpn, "fault": fal, "params": dict(rsk[0]["params"]),
               "risk": "high" if sum(rkx["weight"] for rkx in rsk) >= 3 else "medium", "rank": idx,
               "reason": f"code review: {cit}"[:600], "hypothesis": rsk[0]["hypothesis"]}
        if guard.approve(itm, dep, mock=True):
            continue
        see.add(dpn)
        LAST[itm["id"]] = rsk
        pln.append(itm)
        if len(pln) == top:
            break
    return pln


def risks_for(eid):
    """Code risks behind a planned experiment (for the analyst)."""
    return LAST.get(eid, [])
