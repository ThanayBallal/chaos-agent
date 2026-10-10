"""P4: findings -> docker-compose override + code suggestions (data/patch.json). Robot-shop source is never edited.

patch.json:
{"source": "llm" | "template", "allowed_keys": [...],
 "changes": [{"finding": 1, "experiment": "e1", "service": "redis", "kind": "state_loss",
              "set": {"restart": "unless-stopped", "command": [...], "volumes": ["redis-data:/data"]}, "why": "..."}],
 "code_suggestions": [{"finding": 1, "service": "cart", "file": "cart/server.js", "line": 379, "suggestion": "..."}],
 "override": {"services": {"redis": {...merged set...}}, "volumes": {"redis-data": {}}}}
"override" is ready to write as docker-compose.override.yml. Compose changes only use ALLOWED keys and depmap services;
code changes (timeouts, retries, health flags) only appear as suggestions that cite a code-review file:line.
LLM first (validated), else templates per weakness kind. Run standalone: python -m agent.patcher
"""
import json
import re
from pathlib import Path

from agent import code_review, llm

ROT = Path(__file__).resolve().parent.parent
OUT = ROT / "data" / "patch.json"
ALLOWED = ("restart", "command", "environment", "healthcheck", "volumes")
RESTART = ("no", "always", "on-failure", "unless-stopped")
VOL = re.compile(r"^[a-z0-9][a-z0-9_.-]*:/[^\s:]*(:ro)?$")
ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
HEALTH = {"redis": ["CMD", "redis-cli", "ping"],
          "mongodb": ["CMD", "mongo", "--quiet", "--eval", "db.adminCommand('ping')"],
          "mysql": ["CMD", "mysqladmin", "ping", "-h", "localhost"],
          "rabbitmq": ["CMD", "rabbitmq-diagnostics", "-q", "ping"]}
PERSIST = {"redis": {"command": ["redis-server", "--appendonly", "yes"], "volumes": ["redis-data:/data"]},
           "mongodb": {"volumes": ["mongodb-data:/data/db"]},
           "mysql": {"volumes": ["mysql-data:/data"]}}
KINDS = [("state_loss", r"data loss|lost data|state|persisten|no volume|only copy"),
         ("stale_health", r"never reset|health flag|reports? .*connected"),
         ("no_timeout", r"timeout|hang|blocks? the worker"),
         ("retry_once", r"retry loop|first connect|reconnect"),
         ("logs_only", r"only logs|no fallback")]

SYS = f"""You turn chaos-engineering findings for the Docker Compose app "robot-shop" into remediation.
You get the findings, the experiments, the code-review weaknesses behind them (file:line) and the services.

Return:
- changes: docker-compose override entries. Each names a service from the list and a "set" that uses ONLY these keys:
  {list(ALLOWED)}. restart is one of {list(RESTART)}; command is a list of strings; volumes are NAMED volumes
  "name:/container/path" (no host paths); environment is a list of "KEY=VALUE"; healthcheck is {{"test": ["CMD", ...]}}.
  Examples: redis state loss -> command ["redis-server","--appendonly","yes"], volumes ["redis-data:/data"],
  restart "unless-stopped". A stale health flag or a killed dependency -> restart "unless-stopped" (+ a healthcheck).
- code_suggestions: changes that need application code (timeouts, retries with backoff, resetting health flags).
  Cite file and line exactly as given in code_weaknesses; use file "" and line 0 when no weakness is cited.
Never edit robot-shop source. Never invent services, files or lines. Every finding should get at least one entry."""


def kind(txt):
    low = txt.lower()
    return next((nam for nam, pat in KINDS if re.search(pat, low)), "other")


def _risks(exp):
    """Code-review weaknesses behind an experiment: this run's, else from data/code_review.json by target+fault."""
    rsk = code_review.risks_for(exp.get("id"))
    if rsk:
        return rsk
    try:
        rev = json.loads(code_review.OUT.read_text())
    except (OSError, ValueError):
        return []
    return [itm for itm in rev.get("risks", [])
            if itm.get("dependency") == exp.get("target") and itm.get("fault") == exp.get("fault")]


def _context(fnd, pln):
    pex = {exp["id"]: exp for exp in pln or []}
    return [(itm, pex.get(itm["experiment"], {"id": itm["experiment"], "target": itm["target"]}),
             _risks(pex.get(itm["experiment"], {"id": itm["experiment"], "target": itm["target"]})))
            for itm in fnd.get("findings", [])]


def templates(fnd, pln, dep):
    """Deterministic changes per weakness kind."""
    nam = {svc["name"] for svc in dep["services"]}
    chg, sug = [], []
    for itm, exp, rsk in _context(fnd, pln):
        tgt, rnk = itm["target"], itm["rank"]
        knd = {kind(itm["weakness"])} | {kind(rkx["weakness"]) for rkx in rsk}
        add = lambda knd_, sett, why: chg.append({"finding": rnk, "experiment": itm["experiment"], "service": tgt,
                                                   "kind": knd_, "set": sett, "why": why})
        if tgt in nam:
            if "state_loss" in knd and tgt in PERSIST:
                add("state_loss", {"restart": "unless-stopped", **PERSIST[tgt]},
                    f"{tgt} loses state on restart: persist it on a named volume")
            elif knd & {"stale_health", "retry_once", "logs_only", "state_loss"} or exp.get("fault") == "kill":
                add("dependency_down", {"restart": "unless-stopped"}, f"bring {tgt} back automatically after it dies")
            if tgt in HEALTH and (exp.get("fault") in ("kill", "pause") or "stale_health" in knd):
                add("healthcheck", {"healthcheck": {"test": list(HEALTH[tgt])}},
                    f"report {tgt} unhealthy when it stops answering")
        for rkx in rsk[:3]:
            sug.append({"finding": rnk, "service": rkx["service"], "file": rkx["file"], "line": rkx["line"],
                        "suggestion": rkx["fix"]})
        if not rsk and "no_timeout" in knd:
            sug.append({"finding": rnk, "service": tgt, "file": "", "line": 0,
                        "suggestion": f"callers of {tgt}: set connect/read timeouts and retry with backoff"})
    return chg, sug


def schema(dep):
    nam = sorted(svc["name"] for svc in dep["services"])
    txt, num, lst = {"type": "string"}, {"type": "integer"}, {"type": "array", "items": {"type": "string"}}
    sett = {"type": "object", "additionalProperties": False,
            "properties": {"restart": {"type": "string", "enum": list(RESTART)}, "command": lst, "volumes": lst,
                           "environment": lst,
                           "healthcheck": {"type": "object", "additionalProperties": False, "required": ["test"],
                                           "properties": {"test": lst}}}}
    chg = {"type": "object", "additionalProperties": False, "required": ["finding", "service", "set", "why"],
           "properties": {"finding": num, "service": {"type": "string", "enum": nam}, "set": sett, "why": txt}}
    sug = {"type": "object", "additionalProperties": False,
           "required": ["finding", "service", "file", "line", "suggestion"],
           "properties": {"finding": num, "service": {"type": "string", "enum": nam}, "file": txt, "line": num,
                          "suggestion": txt}}
    return {"type": "object", "additionalProperties": False, "required": ["changes", "code_suggestions"],
            "properties": {"changes": {"type": "array", "items": chg},
                           "code_suggestions": {"type": "array", "items": sug}}}


def check_set(sett):
    """Raise ValueError unless sett is a non-empty compose override using only allowed keys and safe values."""
    if not isinstance(sett, dict) or not sett:
        raise ValueError("'set' must be a non-empty object")
    bad = sorted(set(sett) - set(ALLOWED))
    if bad:
        raise ValueError(f"keys {bad} not allowed; use only {list(ALLOWED)}")
    if "restart" in sett and sett["restart"] not in RESTART:
        raise ValueError(f"restart must be one of {list(RESTART)}")
    for key in ("command", "volumes", "environment"):
        if key in sett and (not isinstance(sett[key], list) or not sett[key]
                            or not all(isinstance(val, str) and val for val in sett[key])):
            raise ValueError(f"{key} must be a non-empty list of strings")
    for vol in sett.get("volumes", []):
        if not VOL.match(vol):
            raise ValueError(f"volume '{vol}' must be a named volume 'name:/path'")
    for env in sett.get("environment", []):
        if not ENV.match(env):
            raise ValueError(f"environment '{env}' must be KEY=VALUE")
    if "healthcheck" in sett:
        hck = sett["healthcheck"]
        tst = hck.get("test") if isinstance(hck, dict) else None
        if set(hck or {}) != {"test"} or not isinstance(tst, list) or len(tst) < 2 \
                or tst[0] not in ("CMD", "CMD-SHELL") or not all(isinstance(val, str) for val in tst):
            raise ValueError('healthcheck must be {"test": ["CMD" | "CMD-SHELL", ...]}')
    return sett


def validate(raw, fnd, pln, dep):
    """Check an LLM reply; return (changes, suggestions) or raise ValueError naming the first problem."""
    if not isinstance(raw, dict) or not isinstance(raw.get("changes"), list) \
            or not isinstance(raw.get("code_suggestions"), list):
        raise ValueError("reply must have 'changes' and 'code_suggestions' lists")
    nam = {svc["name"] for svc in dep["services"]}
    ctx = {itm["rank"]: (itm, exp, rsk) for itm, exp, rsk in _context(fnd, pln)}
    chg, sug = [], []
    for idx, itm in enumerate(raw["changes"], 1):
        if not isinstance(itm, dict) or itm.get("finding") not in ctx:
            raise ValueError(f"change {idx}: 'finding' must be one of the finding ranks {sorted(ctx)}")
        if itm.get("service") not in nam:
            raise ValueError(f"change {idx}: service {itm.get('service')!r} is not in the dependency map")
        try:
            sett = check_set(itm.get("set"))
        except ValueError as err:
            raise ValueError(f"change {idx} ({itm['service']}): {err}") from None
        fin = ctx[itm["finding"]][0]
        chg.append({"finding": itm["finding"], "experiment": fin["experiment"], "service": itm["service"],
                    "kind": kind(f"{fin['weakness']} {itm.get('why', '')}"), "set": sett,
                    "why": str(itm.get("why", "")).strip()})
    for idx, itm in enumerate(raw["code_suggestions"], 1):
        if not isinstance(itm, dict) or itm.get("finding") not in ctx:
            raise ValueError(f"code suggestion {idx}: 'finding' must be one of {sorted(ctx)}")
        cit = {(rkx["file"], rkx["line"]) for rkx in ctx[itm["finding"]][2]}
        fil, lin = itm.get("file") or "", itm.get("line") or 0
        if fil and (fil, lin) not in cit:
            raise ValueError(f"code suggestion {idx}: {fil}:{lin} is not a cited code weakness for finding "
                             f"{itm['finding']}; use one of {sorted(cit)} or file \"\" and line 0")
        if not str(itm.get("suggestion", "")).strip() or itm.get("service") not in nam:
            raise ValueError(f"code suggestion {idx}: needs a known service and a suggestion")
        sug.append({"finding": itm["finding"], "service": itm["service"], "file": fil, "line": lin,
                    "suggestion": itm["suggestion"].strip()})
    if not chg:
        raise ValueError("at least one compose change is required")
    return chg, sug


def override(chg):
    """Merge changes per service into a docker-compose override dict."""
    svc, vol = {}, {}
    for itm in chg:
        cur = svc.setdefault(itm["service"], {})
        for key, val in itm["set"].items():
            if key in ("volumes", "environment"):
                cur[key] = cur.get(key, []) + [one for one in val if one not in cur.get(key, [])]
            else:
                cur[key] = val
        for val in itm["set"].get("volumes", []):
            vol[val.split(":", 1)[0]] = {}
    out = {"services": svc}
    if vol:
        out["volumes"] = vol
    return out


def prompt(fnd, pln, dep):
    dat = {"services": [{key: svc.get(key) for key in ("name", "kind", "depends_on")} for svc in dep["services"]],
           "findings": [{"finding": itm["rank"], "severity": itm["severity"], "target": itm["target"],
                         "experiment": {key: exp.get(key) for key in ("id", "fault", "target")},
                         "weakness": itm["weakness"], "evidence": itm["evidence"], "fix": itm["fix"],
                         "code_weaknesses": [{key: rkx[key] for key in ("service", "file", "line", "weakness", "fix")}
                                             for rkx in rsk]}
                        for itm, exp, rsk in _context(fnd, pln)]}
    return f"Return {{\"changes\": [...], \"code_suggestions\": [...]}}.\n\n{json.dumps(dat, indent=1)}"


def make(fnd, pln, dep, save=True):
    """Return patch.json for these findings (LLM, else templates) and write it to data/patch.json."""
    if not fnd.get("findings"):
        chg, sug, src = [], [], "template"
    else:
        try:
            chg, sug = llm.ask_json(SYS, prompt(fnd, pln, dep), schema=schema(dep),
                                    check=lambda raw: validate(raw, fnd, pln, dep))
            src = "llm"
            print(f"[patcher] LLM patch: {len(chg)} compose changes, {len(sug)} code suggestions")
        except Exception as err:
            chg, sug = templates(fnd, pln, dep)
            src = "template"
            print(f"[patcher] LLM patch unavailable ({str(err)[:120]}); "
                  f"templates: {len(chg)} compose changes, {len(sug)} code suggestions")
    pch = {"source": src, "allowed_keys": list(ALLOWED), "changes": chg, "code_suggestions": sug,
           "override": override(chg)}
    if save:
        try:
            OUT.parent.mkdir(exist_ok=True)
            OUT.write_text(json.dumps(pch, indent=2))
        except OSError as err:
            print(f"[patcher] could not write {OUT}: {err}")
    return pch


if __name__ == "__main__":
    dat = ROT / "data"
    dep_ = dat / "depmap.json" if (dat / "depmap.json").exists() else dat / "mock_depmap.json"
    make(json.loads((dat / "findings.json").read_text()), json.loads((dat / "plan.json").read_text()),
         json.loads(dep_.read_text()))
    print(f"[patcher] wrote {OUT}")
