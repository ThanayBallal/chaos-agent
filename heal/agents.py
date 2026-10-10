"""LLM agents of the heal loop (agent.llm.ask_json), each with its rule-based fallback.

  chaos:       plan_experiment(prompt, risks, dep)           -> (experiment, meta)
  chaos auto:  plan_auto(weaknesses(risks, dep), dep)        -> (experiment, meta, skipped)   heal.run --auto
  diagnosis:   diagnose(brf, dep, cap=None)                  -> (diagnosis, meta)   BLIND
  remediation: remediate(dx, dep, history=None, cap=None)    -> ({"actions", "reason"}, meta)

meta = {"source": "llm" | "fallback", "why": None | why the LLM answer was not used}. The fallback runs when the LLM
is unavailable (CHAOS_NO_LLM, no key, timeout, quota) or its output is still invalid after ask_json's retries.
Every validator raises ValueError, which ask_json feeds back to the model as the reason for a retry.

The chaos agent must honour what the prompt names ("kill redis" -> kill redis); only a vague prompt may fall back to
the top code-review risk, and the reason then says so. Every experiment passes guard.approve.
Diagnosis is BLIND: it receives brief(symptoms, baseline) and the depmap, never the experiment; the graph scores it
against the experiment afterwards (score). Remediation sees the diagnosis and the heal catalog only; restart_web is
appended automatically after a backend start.
Prompts are compact JSON (free-tier quota): a healthy run is about 3 calls (chaos, diagnosis, remediation).
"""
import json
import re

from agent import llm
from agent.planner import DEF, HYP, PRM
from heal import ports
from monitor.live_check import CPU_BUSY
from safety import guard
from safety.allowlist import FAULTS, MAX_DUR, P95_MAX

# ---- heal catalog ----
WEB = "web"  # nginx proxy: caches backend IPs, so it is restarted after any backend restart
HEAL = {"kill": "start_service", "pause": "unpause_service", "latency": "remove_latency",
        "cpu_stress": "stop_cpu_stress"}
HEAL_CATALOG = (*HEAL.values(), "restart_web")
HEAL_DESC = {"start_service": "start a stopped / exited (killed or crashed) container",
             "unpause_service": "unpause a paused (frozen) container",
             "remove_latency": "delete the netem qdisc that delays the container's network traffic",
             "stop_cpu_stress": "kill the busy-loop processes burning CPU inside the container",
             "restart_web": "restart the nginx proxy 'web'; added automatically after a backend start "
                            "(nginx caches backend IPs), do not list it yourself"}
SWEEP = ("unpause_service", "remove_latency", "stop_cpu_stress", "start_service")  # unknown fault: every safe undo

GUESS = (*FAULTS, "unknown")
WORDS = {"kill": ("kill", "stop", "crash", "terminat"), "pause": ("pause", "freez", "frozen", "suspend"),
         "latency": ("latency", "delay", "slow", "lag"), "cpu_stress": ("cpu", "stress", "hog")}
ROLES = {"cache": r"\bcach", "db": r"\b(?:database|datastore|dbs?\b)", "queue": r"\b(?:queue|broker)",
         "proxy": r"\b(?:proxy|gateway)"}  # role words in a prompt -> depmap kind
DOWN = ("exited", "dead", "created", "removing", "restarting")  # container states that mean it is not running
LOG_N, LOG_W = ports.LOG_N, ports.LOG_W  # log lines per unhealthy service, characters per line
TOP_RISKS = 5
MIN_CAP = 8  # seconds: below this, a heal agent skips the LLM (a shorter call times out anyway and wastes quota)
DFL_EXP = {"id": "e1", "target": "redis", "fault": "kill", "params": {"duration_s": 30}, "risk": "high", "rank": 1,
           "reason": "prompt named no target or fault and the code review found no risk: default experiment",
           "hypothesis": "cart requests fail while redis is down; recovers after restart"}


def log(nam, msg):
    print(f"[heal.agents] {nam}: {msg}")


def names(dep):
    return sorted(svc["name"] for svc in dep.get("services", []))


def services(dep):
    """Compact depmap for prompts."""
    return [{key: svc.get(key) for key in ("name", "kind", "depends_on")} for svc in dep.get("services", [])]


def _ask(nam, sys_prompt, task, dat, schema, check, cap=None):
    """(obj, meta) from the LLM, or (None, fallback meta) on any failure."""
    if cap is not None and cap < MIN_CAP:
        why = "heal deadline near: no time for an LLM call"
    else:
        try:
            obj = llm.ask_json(sys_prompt, f"{task}\n{json.dumps(dat, separators=(',', ':'))}", schema=schema,
                               check=check, cap=cap)
            return obj, {"source": "llm", "why": None}
        except Exception as err:
            why = f"{type(err).__name__}: {err}"[:200]
    log(nam, f"LLM not used ({why}); rule-based fallback")
    return None, {"source": "fallback", "why": why}


# ---- 1. chaos agent ----

def _uses(dep, svc):
    """Services svc depends on (depends_on and edges)."""
    out = {dst for itm in dep.get("services", []) if itm["name"] == svc for dst in itm.get("depends_on") or []}
    return out | {edg["dst"] for edg in dep.get("edges", []) if edg.get("src") == svc}


def explicit(prompt, dep):
    """What the prompt asks for: {"named": [the target must be one of these], "fault": fault or None (None if zero or
    several match)}. A role word (cache, database, queue, proxy) resolves to services of that kind: "the redis cache"
    -> redis, "the cache that cart uses" / "the database behind the catalogue" -> the dependency of that kind,
    "pause the database" -> every database."""
    low = (prompt or "").lower()
    nam = [svc for svc in names(dep) if re.search(rf"\b{re.escape(svc.lower())}\b", low)]
    fal = [fal for fal, wds in WORDS.items() if re.search(rf"\b(?:{'|'.join(wds)})", low)]
    rol = {knd for knd, pat in ROLES.items() if re.search(pat, low)}
    if rol:
        ofk = {svc["name"] for svc in dep.get("services", []) if svc.get("kind") in rol}
        own = [svc for svc in nam if svc in ofk]
        use = sorted(set().union(set(), *(_uses(dep, svc) for svc in nam)) & ofk)
        if own:
            nam = own
        elif nam and use:
            nam = use
        elif not nam:
            nam = sorted(ofk)
    return {"named": nam, "fault": fal[0] if len(fal) == 1 else None}


def top_risks(risks, dep):
    """Code-review risks on depmap services with an allowlisted fault, highest weight first."""
    nam = set(names(dep))
    rsk = [itm for itm in risks or [] if itm.get("dependency") in nam and itm.get("fault") in FAULTS]
    return sorted(rsk, key=lambda itm: -(itm.get("weight") or 0))


def _cite(rsk):
    return f"{rsk.get('file')}:{rsk.get('line')} {rsk.get('weakness')}"


def _vague_reason(rsk, top, reason):
    """Reason for a vague prompt whose pick is a code-review risk: it must say so."""
    if rsk is None or "code review" in reason.lower() or "code-review" in reason.lower():
        return reason
    lbl = "the top code-review risk" if rsk is top[0] else "a code-review risk"
    return f"prompt named no target or fault, so chose {lbl} ({_cite(rsk)}): {reason}"[:600]


def fallback_experiment(prompt, risks, dep):
    """Rule-based: what the prompt names wins; else the top code-review risk; else DFL_EXP."""
    exq, top = explicit(prompt, dep), top_risks(risks, dep)
    if not exq["named"] and not exq["fault"]:
        if not top:
            return {**DFL_EXP, "params": dict(DFL_EXP["params"])}
        rsk = top[0]
        return {"id": "e1", "target": rsk["dependency"], "fault": rsk["fault"], "params": dict(rsk["params"]),
                "risk": "high", "rank": 1, "hypothesis": rsk["hypothesis"],
                "reason": f"prompt named no target or fault, so chose the top code-review risk: {_cite(rsk)}"[:600]}
    fit = [itm for itm in top if (not exq["named"] or itm["dependency"] in exq["named"])
           and (not exq["fault"] or itm["fault"] == exq["fault"])]
    knd = {svc["name"]: svc.get("kind") for svc in dep.get("services", [])}
    tgt = exq["named"][0] if exq["named"] else (fit[0]["dependency"] if fit else DFL_EXP["target"])
    fal = exq["fault"] or (fit[0]["fault"] if fit else DEF.get(knd.get(tgt), "kill"))
    rsk = next((itm for itm in fit if itm["dependency"] == tgt and itm["fault"] == fal), None)
    why = f"the prompt asked for: {prompt.strip()!r}" + (f"; code review: {_cite(rsk)}" if rsk else "")
    return {"id": "e1", "target": tgt, "fault": fal, "params": dict(rsk["params"] if rsk else PRM[fal]),
            "risk": "high", "rank": 1, "reason": why[:600],
            "hypothesis": rsk["hypothesis"] if rsk else HYP[fal].format(nam=tgt)}


CHAOS_SYS = f"""You are the chaos agent of a self-healing loop on the Docker Compose app "robot-shop".
Choose exactly ONE fault-injection experiment.
Rules:
- The user's prompt decides. If it names a service and/or a fault ("kill redis", "slow down mysql", "kill the cache that cart uses"), use exactly that service and fault. The field "prompt_names" lists what was detected in the prompt (a role such as "the cache that cart uses" is already resolved to that dependency); your target must be one of "services" there, and your fault must match it.
- Only if the prompt names neither may you choose yourself: pick the top code-review risk (highest weight first) and say in the reason that the prompt was vague and you chose the top code-review risk (cite its file:line).
- target must be a service from "services". fault and params must come from "faults" (exact keys, positive integers), duration_s <= {MAX_DUR}, latency delay_ms < {P95_MAX}. Honour numbers the prompt gives (duration, delay) within these limits.
- reason: why this experiment, citing the prompt, the code-review risk or the dependency map. hypothesis: a falsifiable prediction of what will be observed while the fault is active and after it is healed. Never invent measurements."""

CHAOS_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["target", "fault", "params", "reason", "hypothesis"],
                "properties": {"target": {"type": "string"}, "fault": {"type": "string", "enum": list(FAULTS)},
                               "params": {"type": "object", "additionalProperties": False, "required": ["duration_s"],
                                          "properties": {key: {"type": "integer"}
                                                         for key in sorted(set().union(*FAULTS.values()))}},
                               "reason": {"type": "string"}, "hypothesis": {"type": "string"}}}


def check_experiment(raw, prompt, risks, dep):
    """Validate an LLM experiment -> plan.json item; ValueError names the first problem (fed back to the LLM)."""
    if not isinstance(raw, dict):
        raise ValueError("reply must be one JSON object {target, fault, params, reason, hypothesis}")
    exq, top = explicit(prompt, dep), top_risks(risks, dep)
    tgt, fal = raw.get("target"), raw.get("fault")
    if fal not in FAULTS:
        raise ValueError(f"fault '{fal}' must be one of {list(FAULTS)}")
    if exq["named"] and tgt not in exq["named"]:
        raise ValueError(f"the user's prompt asks for {exq['named']}; target must be {' or '.join(exq['named'])}")
    if exq["fault"] and fal != exq["fault"]:
        raise ValueError(f"the user's prompt asks for fault '{exq['fault']}', not '{fal}'")
    src = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    prm = {key: src.get(key) for key in sorted(FAULTS[fal])}
    bad = [key for key, val in prm.items() if not isinstance(val, int) or isinstance(val, bool) or val < 1]
    if bad:
        raise ValueError(f"params {bad} must be positive integers; {fal} takes exactly {sorted(FAULTS[fal])}")
    if fal == "latency" and prm["delay_ms"] >= P95_MAX:
        raise ValueError(f"delay_ms must be below {P95_MAX}")
    for key in ("reason", "hypothesis"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f"'{key}' must be a non-empty string")
    exp = {"id": "e1", "target": tgt, "fault": fal, "params": prm, "risk": "high", "rank": 1,
           "reason": raw["reason"].strip()[:600], "hypothesis": raw["hypothesis"].strip()[:600]}
    rej = guard.approve(exp, dep, mock=not ports.live())
    if rej:
        raise ValueError(f"rejected by the safety guard: {rej}")
    if not exq["named"] and not exq["fault"]:
        rsk = next((itm for itm in top if itm["dependency"] == tgt and itm["fault"] == fal), None)
        exp["reason"] = _vague_reason(rsk, top, exp["reason"])
    return exp


def plan_experiment(prompt, risks, dep):
    """One experiment from the prompt, the code-review risks and the depmap -> (exp, meta)."""
    exq = explicit(prompt, dep)
    rsk = [{"dependency": itm["dependency"], "fault": itm["fault"], "params": itm.get("params"),
            "weight": itm.get("weight"), "where": f"{itm.get('file')}:{itm.get('line')}",
            "weakness": str(itm.get("weakness", ""))[:200], "hypothesis": str(itm.get("hypothesis", ""))[:200]}
           for itm in top_risks(risks, dep)[:TOP_RISKS]]
    dat = {"prompt": prompt, "prompt_names": {"services": exq["named"], "fault": exq["fault"]},
           "code_review_risks": rsk, "services": services(dep),
           "faults": {fal: sorted(prm) for fal, prm in FAULTS.items()}, "max_duration_s": MAX_DUR}
    exp, met = _ask("chaos_agent", CHAOS_SYS, "Choose one experiment. Data:", dat, CHAOS_SCHEMA,
                    lambda raw: check_experiment(raw, prompt, risks, dep))
    return (exp if exp else fallback_experiment(prompt, risks, dep)), met


# ---- 1b. chaos agent, autonomous mode (heal.run --auto) ----

AUTO = "auto"  # the chaos agent's prompt in autonomous mode
AUTO_TRY = 3  # weaknesses tried, in rank order, while the guard rejects them


def source_line(rel, num):
    """Line num (1-based) of robot-shop file rel as read from disk, stripped; None if it cannot be read, is blank or
    lies outside the app."""
    from agent import code_review
    try:
        app = code_review.APP.resolve()
        pth = (app / str(rel)).resolve()
        num = int(num)
        if num < 1 or app not in pth.parents:
            return None
        lns = pth.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError, TypeError):
        return None
    return (lns[num - 1].strip()[:160] or None) if num <= len(lns) else None


def weaknesses(risks, dep, top=TOP_RISKS):
    """The ranked code-review weaknesses --auto shows and chooses from: top_risks order, at most top. "code" is the
    cited line read from the file itself (never the review's quote); a weakness whose file:line cannot be read is
    skipped."""
    out = []
    for itm in top_risks(risks, dep):
        cod = source_line(itm.get("file"), itm.get("line"))
        if cod is None:
            log("weaknesses", f"skipped {itm.get('file')}:{itm.get('line')}: line cannot be read from the source")
            continue
        fal, tgt = itm["fault"], itm["dependency"]
        out.append({"rank": len(out) + 1, "service": itm.get("service"), "dependency": tgt, "fault": fal,
                    "params": dict(itm.get("params") or PRM[fal]), "file": itm["file"], "line": int(itm["line"]),
                    "code": cod, "weakness": str(itm.get("weakness", "")).strip(),
                    "hypothesis": str(itm.get("hypothesis") or HYP[fal].format(nam=tgt)).strip(),
                    "weight": itm.get("weight")})
        if len(out) == top:
            break
    return out


def auto_tag(wek, num):
    return f"Chosen autonomously: weakness #{wek['rank']} of {num} from the code review ({wek['file']}:{wek['line']}) because "


def _auto_why(wek, skp):
    if not skp:
        return f"it is the top-ranked weakness (weight {wek['weight']}): {wek['weakness']}"
    return (f"weakness{'es' if len(skp) > 1 else ''} {', '.join('#' + str(itm['rank']) for itm in skp)} "
            f"{'were' if len(skp) > 1 else 'was'} rejected by the safety guard and it is the next-ranked: "
            f"{wek['weakness']}")


def _campaign_why(wek):
    return f"the campaign tests each ranked weakness in turn (weight {wek['weight']}): {wek['weakness']}"


def auto_fallback(wek, num, skp=(), why=None):
    """Rule-based experiment for weakness wek, with the autonomous reason (why replaces the default explanation)."""
    return {"id": "e1", "target": wek["dependency"], "fault": wek["fault"], "params": dict(wek["params"]),
            "risk": "high", "rank": 1, "reason": (auto_tag(wek, num) + (why or _auto_why(wek, skp)))[:600],
            "hypothesis": wek["hypothesis"][:600]}


CHAOS_AUTO_SYS = f"""You are the chaos agent of a self-healing loop on the Docker Compose app "robot-shop", in autonomous mode: there is no user prompt.
You test ONE code-review weakness, given as "weakness". target and fault are fixed: use exactly weakness.dependency and weakness.fault.
- params must come from "faults" for that fault (exact keys, positive integers), duration_s <= {MAX_DUR}, latency delay_ms < {P95_MAX}; weakness.params is a good default.
- reason: one sentence saying why this weakness is worth testing, citing the quoted code. Do not mention its rank (it is added for you).
- hypothesis: a falsifiable prediction of what will be observed while the fault is active and after it is healed. Never invent measurements."""


def check_auto(raw, wek, num, dep):
    """Validate an autonomous LLM experiment: weakness wek's target and fault, then check_experiment's rules; the
    reason gets the autonomous prefix."""
    if isinstance(raw, dict) and (raw.get("target"), raw.get("fault")) != (wek["dependency"], wek["fault"]):
        raise ValueError(f"autonomous mode tests weakness #{wek['rank']}: target must be '{wek['dependency']}' and "
                         f"fault '{wek['fault']}'")
    exp = check_experiment(raw, AUTO, [], dep)
    why = re.sub(r"^(?:chosen autonomously[^:]*:\s*)?(?:because\s+)?", "", raw["reason"].strip(), flags=re.I)
    return {**exp, "reason": (auto_tag(wek, num) + why)[:600]}


def plan_auto(wks, dep):
    """--auto: an experiment for the top-ranked weakness the guard approves, trying at most AUTO_TRY in rank order.
    The guard is asked before the LLM, so a rejected weakness costs no LLM call; an LLM answer the guard rejects is
    replaced by the rule-based one. -> (exp, meta, skipped); exp carries "rejection" if nothing could be approved."""
    num, skp = len(wks), []
    for wek in wks[:AUTO_TRY]:
        exp, met, rej = _plan_one(wek, num, dep, auto_fallback(wek, num, skp))
        if rej:
            skp.append(skipped(wek, rej))
            continue
        return exp, met, skp
    why = (f"the safety guard rejected the top {len(skp)} weaknesses" if wks
           else "no code-review weakness could be read from the source")
    exp = auto_fallback(wks[len(skp) - 1], num, skp[:-1]) if skp else {**DFL_EXP, "params": dict(DFL_EXP["params"])}
    return {**exp, "rejection": f"autonomous mode: {why}"}, {"source": "fallback", "why": why}, skp


def skipped(wek, rej):
    """A weakness the safety guard rejected, as recorded in the report's auto.skipped."""
    log("chaos_agent", f"auto: skipped weakness #{wek['rank']} ({wek['fault']} {wek['dependency']}): {rej}")
    return {"rank": wek["rank"], "where": f"{wek['file']}:{wek['line']}",
            "experiment": f"{wek['fault']} {wek['dependency']}", "why": f"rejected by the safety guard: {rej}"}


def _plan_one(wek, num, dep, exp):
    """Guard first (a rejected weakness costs no LLM call), then the LLM; exp is the rule-based fallback.
    -> (exp, meta, rejection or None)."""
    rej = guard.approve(exp, dep, mock=not ports.live())
    if rej:
        return exp, {"source": "fallback", "why": "rejected by the safety guard"}, rej
    dat = {"weakness": {key: wek[key] for key in ("service", "dependency", "fault", "params", "file", "line", "code",
                                                   "weakness", "hypothesis")},
           "services": services(dep), "faults": {fal: sorted(prm) for fal, prm in FAULTS.items()},
           "max_duration_s": MAX_DUR}
    out, met = _ask("chaos_agent", CHAOS_AUTO_SYS, "Plan the experiment for this weakness. Data:", dat, CHAOS_SCHEMA,
                    lambda raw: check_auto(raw, wek, num, dep))
    return (out or exp), met, None


def plan_pick(wek, num, dep):
    """--auto --all: an experiment for campaign weakness wek (#rank of num), no fallback to another weakness.
    -> (exp, meta, skipped or None); exp carries "rejection" if the guard rejected it."""
    exp, met, rej = _plan_one(wek, num, dep, auto_fallback(wek, num, why=_campaign_why(wek)))
    if rej:
        return {**exp, "rejection": f"autonomous campaign: weakness #{wek['rank']} rejected by the safety guard: {rej}"}, \
            met, skipped(wek, rej)
    return exp, met, None


# ---- 2. diagnosis agent (blind) ----

def brief(sym, bas):
    """The ONLY symptoms the diagnosis agent sees: measured values from a whitelist of keys, compact. Never the
    experiment's id/target/fault/reason/hypothesis, and never `observations` (live ones start with the fault)."""
    msd = sym.get("measured") or {}
    hlt = msd.get("health") or {}
    bad = {svc: sta for svc, sta in sorted(hlt.items()) if sta != "healthy"}
    con = {svc: sta for svc, sta in sorted((msd.get("containers") or {}).items()) if sta != "running"}
    return {"error_rate": {"baseline": bas.get("error_rate"), "now": sym.get("error_rate")},
            "p95_ms": {"baseline": bas.get("p95_ms"), "now": sym.get("p95_ms")},
            "failing_endpoints": {nam: f"{eps.get('errors')}/{eps.get('requests')} failed, p95 {eps.get('p95_ms')}ms"
                                  for nam, eps in sorted((msd.get("endpoints") or {}).items()) if eps.get("errors")},
            "unhealthy": bad, "healthy_count": len(hlt) - len(bad), "containers_not_running": con,
            "cpu_busy_pct": {svc: val for svc, val in sorted((msd.get("cpu_pct") or {}).items())
                             if val is not None and val >= CPU_BUSY},
            "netem": sorted(msd.get("netem") or []),
            "logs": {svc: [str(lin)[:LOG_W] for lin in list(lns)[-LOG_N:]]
                     for svc, lns in sorted((msd.get("logs") or {}).items()) if svc in bad or svc in con}}


DIAG_SYS = f"""You are the diagnosis agent of a self-healing loop on the Docker Compose app "robot-shop".
Something is wrong with the app. You are given ONLY measured symptoms (vs baseline) and the dependency map; you are NOT told what was done to it.
Find the root-cause service and guess the fault type:
- kill: the container is exited / not running (dependents fail to connect)
- pause: the container is paused / frozen (requests hang or time out)
- latency: a netem qdisc delays the container's network (p95 rises, errors only on timeouts)
- cpu_stress: the container burns CPU (cpu_busy_pct high, p95 rises)
- unknown: the symptoms do not support a guess
The root cause is usually the dependency that the unhealthy and failing services share, not the callers that fail because of it.
evidence: short strings, each citing a measured symptom you were given. confidence: 0..1.
suspect_service must be a depmap service, or null only with fault_guess "unknown". affected_services: depmap services that are degraded."""

DIAG_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["suspect_service", "fault_guess", "affected_services", "evidence", "confidence"],
               "properties": {"suspect_service": {"type": ["string", "null"]},
                              "fault_guess": {"type": "string", "enum": list(GUESS)},
                              "affected_services": {"type": "array", "items": {"type": "string"}},
                              "evidence": {"type": "array", "items": {"type": "string"}},
                              "confidence": {"type": "number"}}}
DX_KEYS = tuple(DIAG_SCHEMA["required"])


def check_diagnosis(raw, dep):
    """Validate an LLM diagnosis; ValueError names the first problem."""
    if not isinstance(raw, dict):
        raise ValueError(f"reply must be one JSON object with {list(DX_KEYS)}")
    nam = set(names(dep))
    sus, fal = raw.get("suspect_service") or None, raw.get("fault_guess")
    if fal not in GUESS:
        raise ValueError(f"fault_guess '{fal}' must be one of {list(GUESS)}")
    if sus is None and fal != "unknown":
        raise ValueError("suspect_service may be null only when fault_guess is 'unknown'")
    if sus is not None and sus not in nam:
        raise ValueError(f"suspect_service '{sus}' is not a depmap service {sorted(nam)}")
    aff = raw.get("affected_services")
    if not isinstance(aff, list) or [svc for svc in aff if svc not in nam]:
        raise ValueError(f"affected_services must be a list of depmap services {sorted(nam)}")
    evd = raw.get("evidence")
    if not isinstance(evd, list) or not evd or not all(isinstance(itm, str) and itm.strip() for itm in evd):
        raise ValueError("evidence must be a non-empty list of strings citing the measured symptoms")
    con = raw.get("confidence")
    if not isinstance(con, (int, float)) or isinstance(con, bool):
        raise ValueError("confidence must be a number 0..1")
    return {"suspect_service": sus, "fault_guess": fal, "affected_services": list(dict.fromkeys(aff)),
            "evidence": [itm.strip()[:300] for itm in evd][:8], "confidence": round(min(max(con, 0.0), 1.0), 2)}


def rule_diagnosis(brf):
    """Blind rule fallback: exited -> kill, paused -> pause, netem -> latency, busy cpu -> cpu_stress, else unknown."""
    con, bad = brf.get("containers_not_running") or {}, brf.get("unhealthy") or {}
    cpu = brf.get("cpu_busy_pct") or {}
    sig = ([(svc, "kill", f"{svc} container {sta}") for svc, sta in con.items() if sta in DOWN]
           + [(svc, "pause", f"{svc} paused") for svc in sorted(set(con) | set(bad))
              if "paused" in (con.get(svc), bad.get(svc))]
           + [(svc, "latency", f"{svc} has a netem qdisc") for svc in brf.get("netem") or []]
           + [(svc, "cpu_stress", f"{svc} cpu {val}% of one core")
              for svc, val in sorted(cpu.items(), key=lambda kv: -kv[1])])
    err, p95 = brf.get("error_rate") or {}, brf.get("p95_ms") or {}
    evd = [txt for _, _, txt in sig[:3]] + [f"{svc} {sta}" for svc, sta in bad.items()][:3] + [
        f"error_rate {err.get('baseline')} baseline -> {err.get('now')}",
        f"p95 {p95.get('baseline')}ms baseline -> {p95.get('now')}ms"]
    if sig:
        sus, fal, cnf = sig[0][0], sig[0][1], 0.8
    elif bad:
        sus, fal, cnf = next(iter(bad)), "unknown", 0.3
    else:
        sus, fal, cnf = None, "unknown", 0.0
    aff = list(dict.fromkeys(([sus] if sus else []) + list(bad)))
    return {"suspect_service": sus, "fault_guess": fal, "affected_services": aff, "evidence": evd, "confidence": cnf}


def diagnose(brf, dep, cap=None):
    """BLIND diagnosis from brief() and the depmap only -> (diagnosis, meta). It is never given the experiment."""
    dat = {"symptoms": brf, "services": services(dep)}
    dx, met = _ask("diagnosis_agent", DIAG_SYS, "Diagnose. Data:", dat, DIAG_SCHEMA,
                   lambda raw: check_diagnosis(raw, dep), cap)
    return (dx if dx else rule_diagnosis(brf)), met


def score(dx, exp):
    """Diagnosis vs the real experiment, computed only after the diagnosis is final."""
    svc, fal = dx.get("suspect_service") == exp.get("target"), dx.get("fault_guess") == exp.get("fault")
    return {"correct": svc and fal, "service": svc, "fault": fal}


# ---- 3. remediation agent ----

def heal_plan(fault, target):
    """Catalog actions that undo `fault` on `target`; restart_web last whenever a backend was restarted."""
    return with_web([{"action": HEAL[fault], "target": target}])


def with_web(act):
    """Drop any restart_web, then append restart_web(web) once if a backend (not web) is started."""
    out = [itm for itm in act if itm["action"] != "restart_web"]
    if any(itm["action"] == "start_service" and itm["target"] != WEB for itm in out):
        out.append({"action": "restart_web", "target": WEB})
    return out


def check_heal(act):
    """Raise ValueError unless every action is in HEAL_CATALOG and restart_web is last after a backend restart."""
    for itm in act:
        if itm.get("action") not in HEAL_CATALOG:
            raise ValueError(f"'{itm.get('action')}' is not a heal action {HEAL_CATALOG}; "
                             f"hardening patches belong in harden_agent")
    web = [idx for idx, itm in enumerate(act) if itm["action"] == "restart_web"]
    if any(itm["action"] == "start_service" and itm.get("target") != WEB for itm in act) and web != [len(act) - 1]:
        raise ValueError("restart_web must be the last action after a backend restart (nginx caches backend IPs)")
    if web and web != [len(act) - 1]:
        raise ValueError("restart_web may appear once, as the last action")
    return act


REM_SYS = f"""You are the remediation agent of a self-healing loop on the Docker Compose app "robot-shop".
Undo the diagnosed fault NOW, using only actions from "catalog" (exact names), each on a service from "services".
Order matters: e.g. unpause before anything that needs a running process. Do not list restart_web: it is appended automatically after a backend start.
No hardening, config changes or patches: those come later. Keep the list short; when the fault is unknown, choose the safe undos that fit the evidence.
If "previous_attempt" is given, the actions listed there did not heal the app: use its new symptoms to choose differently."""

REM_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["actions", "reason"],
              "properties": {"actions": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["action", "target"],
                  "properties": {"action": {"type": "string", "enum": list(HEAL_CATALOG)},
                                 "target": {"type": "string"}}}},
                  "reason": {"type": "string"}}}


def check_remediation(raw, dep):
    """Validate LLM heal actions -> {"actions", "reason"}; only HEAL_CATALOG actions on depmap services."""
    acts = raw.get("actions") if isinstance(raw, dict) else None
    if not isinstance(acts, list) or not acts:
        raise ValueError("'actions' must be a non-empty list of {action, target}")
    nam, out = set(names(dep)), []
    for itm in acts:
        if not isinstance(itm, dict):
            raise ValueError("each action must be an object {action, target}")
        act, tgt = itm.get("action"), itm.get("target")
        if act not in HEAL_CATALOG:
            raise ValueError(f"'{act}' is not a heal action; use only {list(HEAL_CATALOG)}")
        if act == "restart_web":
            continue
        if tgt not in nam:
            raise ValueError(f"target '{tgt}' is not a depmap service {sorted(nam)}")
        if {"action": act, "target": tgt} in out:
            raise ValueError(f"duplicate action {act}({tgt})")
        out.append({"action": act, "target": tgt})
    if not out:
        raise ValueError("no heal action besides restart_web (which is added automatically)")
    return {"actions": check_heal(with_web(out)), "reason": str(raw.get("reason") or "").strip()[:300]}


def rule_remediation(dx):
    """Rule fallback: the catalog undo for the guessed fault; unknown fault -> every safe undo on the suspect."""
    sus, fal = dx.get("suspect_service"), dx.get("fault_guess")
    if not sus:
        return {"actions": [], "reason": "rule: no suspect service, nothing to undo"}
    if fal in HEAL:
        return {"actions": heal_plan(fal, sus), "reason": f"rule: undo {fal} on {sus}"}
    return {"actions": with_web([{"action": act, "target": sus} for act in SWEEP]),
            "reason": f"rule: fault unknown, every safe undo on {sus}"}


def remediate(dx, dep, history=None, cap=None):
    """Heal actions for diagnosis dx -> ({"actions", "reason"}, meta). history (attempt 2) =
    {"previous_diagnosis", "actions_taken", "new_symptoms"}."""
    dat = {"diagnosis": {key: dx.get(key) for key in DX_KEYS}, "catalog": HEAL_DESC, "services": names(dep)}
    if history:
        dat["previous_attempt"] = history
    pln, met = _ask("remediation_agent", REM_SYS, "Choose heal actions. Data:", dat, REM_SCHEMA,
                    lambda raw: check_remediation(raw, dep), cap)
    if pln:
        return pln, met
    if history and history.get("new_symptoms"):
        new = rule_diagnosis(history["new_symptoms"])
        dx = new if new["suspect_service"] else dx
    return rule_remediation(dx), met
