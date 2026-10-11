"""Turn heal.run's console output into structured events for the dashboard.

The pipeline prints a stable format ("[heal] node: msg", "== Diagnosis (blind): ...", "=== Cycle k/n ..."), so the UI
needs no change to heal/: Parser().feed(line) -> list of event dicts. It keeps a little context so indented
follow-up lines (code:, weakness:, reason:, hypothesis:, evidence:, before -> after:) attach to the right event.
"""
import ast
import re

NODES = ("understand", "chaos_agent", "guard_approve", "inject", "hold", "observe", "diagnosis_agent",
         "remediation_agent", "apply_fix", "verify", "harden_agent", "apply_hardening", "rerun", "compare",
         "revert_patch", "guard_revert", "report")

RNODE = re.compile(r"^\[heal\] (\w+): (.*)$")
RWEAK = re.compile(r"^\s*(\d+)\.\s+(\S+)\s+->\s+(\S+)\s+\((\w+)\)\s+(\S+:\d+)\s*$")
RCYCL = re.compile(r"^=== Cycle (\d+)/(\d+): (\S+) (\w+) ===")
RCHOS = re.compile(r"^\[heal\] chaos_agent: \[(\w+)\] (\w+) (\S+) (\{.*?\}): (.*)$")
REXPR = re.compile(r"^== Experiment: (\w+) (\S+) (\{.*\})\s*$")
RDIAG = re.compile(r"^== Diagnosis \(blind\): suspect=(\S+) fault=(\S+) confidence=([\d.]+) -> (\w+)(.*)$")
RACTN = re.compile(r"^== Action attempt (\d+): (\w+)\((\w+)\) ok=(\w+)\s*(.*)$")
RHEAL = re.compile(r"^== Healed in (\d+)s")
RHARD = re.compile(r"^== Hardening round (\d+): (\w+) (\S+) improved=(\w+)(?: \((.*)\))?\s*$")
RHNA = re.compile(r"^== Hardening: not applicable(?: \((.*)\))?")
RBEFR = re.compile(r"^\s+before -> after: (.*)$")
RSURV = re.compile(r"^== Data survived \(cart probe\): before=(\w+).*?((?:, rerun \d+=\w+.*?)*)$")
RAGNT = re.compile(r"^== Agents: (.*)$")
ROBSV = re.compile(r"observe: (?:attempt \d+: )?error_rate=([\d.]+) p95=(\d+)ms(?: unhealthy=(.*))?$")
RROW = re.compile(r"^(\d+) \| (\S+) \| (\S+) \| (\S+) \| (.*?) \| (.*?) \| (.*?) \| (.*?)\s*$")
RMODE = re.compile(r"^== Mode: (.*)$")
RNOTH = re.compile(r"^== Not healed: (.*)$")
RREVIEW = re.compile(r"^== Code review \((\w+), (\d+) files\)")


def _bool(txt):
    return {"true": True, "false": False}.get(str(txt).strip().lower())


def _lit(txt):
    try:
        return ast.literal_eval(txt)
    except (ValueError, SyntaxError):
        return txt


def _pairs(txt):
    """'error_rate=[0.0, 0.0] recovery_s=[31, 26] data_survived=[False, True] score=85 -> 98' -> dict."""
    out = {}
    for key, val in re.findall(r"(\w+)=(\[[^\]]*\])", txt):
        out[key] = _lit(val)
    scr = re.search(r"score=(\S+) -> (\S+)", txt)
    if scr:
        out["score"] = [_lit(scr.group(1)), _lit(scr.group(2))]
    return out


class Parser:
    def __init__(self):
        self.ctx = None  # which event indented lines attach to: weakness | experiment | diagnosis | hardening
        self.tbl = False  # inside the campaign summary table

    def feed(self, raw):
        lin = raw.rstrip("\r\n")
        out = []
        if not lin.strip():
            return out
        if lin.startswith("=== Campaign summary"):
            self.tbl = True
            out.append({"t": "summary_start", "text": lin.strip("= ").strip()})
            return out
        if self.tbl:
            row = RROW.match(lin)
            if row:
                out.append({"t": "summary_row", "rank": int(row.group(1)), "where": row.group(2),
                            "target": row.group(3), "fault": row.group(4), "correct": row.group(5).strip(),
                            "healed": row.group(6).strip(), "hardening": row.group(7).strip(),
                            "score": row.group(8).strip()})
                return out
            if lin.startswith(("#", "-")):
                return out
            self.tbl = False
        mat = RREVIEW.match(lin)
        if mat:
            return [{"t": "review", "source": mat.group(1), "files": int(mat.group(2))}]
        mat = RWEAK.match(lin)
        if mat:
            self.ctx = "weakness"
            return [{"t": "weakness", "rank": int(mat.group(1)), "service": mat.group(2), "target": mat.group(3),
                     "fault": mat.group(4), "where": mat.group(5)}]
        sub = re.match(r"^\s+(code|weakness|reason|hypothesis|evidence): (.*)$", lin)
        if sub and self.ctx:
            key, val = sub.groups()
            return [{"t": "detail", "of": self.ctx, "key": key, "value": val.strip()}]
        mat = RCYCL.match(lin)
        if mat:
            self.ctx = None
            return [{"t": "cycle", "k": int(mat.group(1)), "n": int(mat.group(2)), "target": mat.group(3),
                     "fault": mat.group(4)}]
        mat = RCHOS.match(lin)
        if mat:
            src, fal, tgt, prm, why = mat.groups()
            out.append({"t": "chosen", "source": src, "fault": fal, "target": tgt, "params": _lit(prm),
                        "reason": why})
        mat = REXPR.match(lin)
        if mat:
            self.ctx = "experiment"
            return [{"t": "experiment", "fault": mat.group(1), "target": mat.group(2), "params": _lit(mat.group(3))}]
        mat = RDIAG.match(lin)
        if mat:
            self.ctx = "diagnosis"
            return [{"t": "diagnosis", "suspect": mat.group(1), "fault": mat.group(2),
                     "confidence": float(mat.group(3)), "correct": mat.group(4) == "CORRECT",
                     "verdict": (mat.group(4) + mat.group(5)).strip()}]
        mat = RACTN.match(lin)
        if mat:
            return [{"t": "action", "attempt": int(mat.group(1)), "action": mat.group(2), "service": mat.group(3),
                     "ok": _bool(mat.group(4)), "detail": mat.group(5)}]
        mat = RHEAL.match(lin)
        if mat:
            return [{"t": "healed", "seconds": int(mat.group(1))}]
        if lin.startswith("== Healed"):
            return [{"t": "healed", "seconds": None}]
        mat = RNOTH.match(lin)
        if mat:
            return [{"t": "not_healed", "why": mat.group(1)}]
        mat = RHARD.match(lin)
        if mat:
            self.ctx = "hardening"
            return [{"t": "hardening", "round": int(mat.group(1)), "fix": mat.group(2), "service": mat.group(3),
                     "improved": _bool(mat.group(4)), "why": mat.group(5) or ""}]
        mat = RHNA.match(lin)
        if mat:
            return [{"t": "hardening_na", "why": mat.group(1) or "no hardening fix available"}]
        mat = RBEFR.match(lin)
        if mat:
            return [{"t": "before_after", **_pairs(mat.group(1))}]
        mat = RSURV.match(lin)
        if mat:
            rrs = [_bool(val) for val in re.findall(r"rerun \d+=(\w+)", mat.group(2) or "")]
            return [{"t": "survived", "before": _bool(mat.group(1)), "reruns": rrs}]
        mat = RAGNT.match(lin)
        if mat:
            ags = {}
            for nam, val in re.findall(r"(\w+_agent)=(\S+?)(?:\s|,|$)", mat.group(1)):
                ags[nam] = "llm" if val.startswith("llm") else "fallback"
            return [{"t": "agents", "agents": ags}]
        mat = RMODE.match(lin)
        if mat:
            return [{"t": "mode", "text": mat.group(1)}]
        if "Press Enter to start healing" in lin:
            out.append({"t": "hold_wait"})
        mat = ROBSV.search(lin)
        if mat and ("[heal.ports] observe" in lin or "[heal] observe" in lin):
            unh = _lit(mat.group(3)) if mat.group(3) and mat.group(3) != "none" else []
            out.append({"t": "metrics", "error_rate": float(mat.group(1)), "p95_ms": int(mat.group(2)),
                        "unhealthy": unh if isinstance(unh, list) else []})
        mat = RNODE.match(lin)
        if mat and mat.group(1) in NODES:
            nod, msg = mat.groups()
            out.append({"t": "node", "node": nod, "msg": msg})
            if nod == "hold" and ("healing starts" in msg or "enter after" in msg):
                out.append({"t": "hold_end"})
            if nod == "inject":
                fal = re.match(r"(\w+) on (\S+):", msg)
                if fal:
                    out.append({"t": "injected", "fault": fal.group(1), "target": fal.group(2)})
        return out
