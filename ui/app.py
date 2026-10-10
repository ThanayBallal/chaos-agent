"""Read-only Streamlit dashboard for Chaos Agent run artifacts."""
from __future__ import annotations

import html
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

st.set_page_config(page_title="Chaos Agent", page_icon="⚡", layout="wide")
st.markdown(
    """
    <style>
      .stApp { background: #10141b; color: #e8edf5; }
      [data-testid="stSidebar"] { background: #171e28; }
      div.stButton > button[kind="primary"] { background: #54d6c7; color: #081312; border: 0; }
      .ca-chip { display: inline-block; margin: .25rem .35rem .25rem 0; padding: .4rem .75rem;
        border-radius: 999px; background: #202b38; border: 1px solid #54d6c7; color: #e8edf5; }
      .ca-arrow { color: #54d6c7; padding: 0 .15rem; }
      .ca-badge { display: inline-block; border-radius: 999px; padding: .12rem .5rem;
        margin-left: .35rem; font-size: .75rem; font-weight: 700; background: #253544; color: #9be9df; }
      .ca-correct { background: #163c2b; color: #91e5aa; border-radius: 999px; padding: .18rem .6rem; font-weight: 700; }
      .ca-wrong { background: #4a2428; color: #ff9da5; border-radius: 999px; padding: .18rem .6rem; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)


def read_data(filename: str) -> tuple[Any | None, str | None]:
    """Read one known JSON artifact from data/, without importing project code."""
    path = DATA / filename
    try:
        with path.open("r", encoding="utf-8") as artifact:
            return json.load(artifact), None
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError) as error:
        return None, str(error)


def pick(mapping: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(mapping, dict):
        for key in keys:
            if key in mapping and mapping[key] is not None:
                return mapping[key]
    return default


def display_value(value: Any) -> str:
    if value is None:
        return "Not run yet"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def section_missing(message: str = "Not run yet") -> None:
    st.info(message)


def llm_badges(report: Any) -> None:
    agents = pick(report, "agents", "agent_sources", default={})
    if not isinstance(agents, dict) or not agents:
        st.caption("Agent source: Not run yet")
        return
    badges = []
    for name, details in agents.items():
        source = details
        if isinstance(details, dict):
            source = pick(details, "source", "mode", "provider", "used", "llm", default=None)
        label = display_value(source)
        normalized = label.lower()
        badge = "LLM" if "llm" in normalized or normalized in {"true", "gemini", "openai"} else "Fallback"
        badges.append(f"**{name}:** <span class='ca-badge'>{html.escape(badge)}</span>")
    st.markdown("&nbsp;&nbsp;".join(badges), unsafe_allow_html=True)


def resolve_experiment(report: Any) -> Any:
    experiment = pick(report, "experiment", "selected_experiment")
    if experiment is not None:
        return experiment
    experiments = pick(report, "experiments", "plan", default=[])
    if isinstance(experiments, list) and experiments:
        return experiments[0]
    return None


def run_live(prompt: str) -> None:
    st.session_state.run_in_progress = True
    output_lines: list[str] = []
    status = st.empty()
    log_area = st.empty()
    try:
        with st.expander("Live run log", expanded=True):
            log_area.code("Starting live run…", language="text")
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "heal.run", prompt, "--live"],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    output_lines.append(line.rstrip("\r\n"))
                    log_area.code("\n".join(output_lines[-500:]), language="text")
                return_code = process.wait()
                if return_code == 0:
                    status.success("Live run completed successfully.")
                else:
                    status.error(f"Live run failed with exit code {return_code}.")
            except OSError as error:
                status.error(f"Could not start the live run: {error}")
                log_area.code("\n".join(output_lines) or "No process output.", language="text")
    finally:
        st.session_state.run_in_progress = False


st.title("⚡ Chaos Agent")
st.caption("Explore resilience experiments, measured outcomes, and hardening results in one place.")

if "run_in_progress" not in st.session_state:
    st.session_state.run_in_progress = False

with st.sidebar:
    st.header("Run an experiment")
    prompt = st.text_input("Chaos prompt", value="kill redis", key="chaos_prompt")
    st.warning("Live runs affect real Robot Shop containers. Use only when you intend to inject a fault.")
    run_clicked = st.button(
        "Run live",
        type="primary",
        disabled=st.session_state.run_in_progress or not prompt.strip(),
        use_container_width=True,
    )
    if run_clicked:
        run_live(prompt.strip())

report, report_error = read_data("heal_report.json")
code_review, review_error = read_data("code_review.json")
depmap, depmap_error = read_data("depmap.json")

st.divider()
st.header("Run summary")
if report_error:
    st.error(f"Could not read data/heal_report.json: {report_error}")
elif not isinstance(report, dict):
    section_missing()
else:
    prompt_value = pick(report, "prompt", "chaos_prompt")
    experiment = resolve_experiment(report)
    diagnosis = pick(report, "blind_diagnosis", "diagnosis", default=None)
    remediation = pick(report, "remediation_actions", "remediation", "actions", default=None)
    hardening = pick(report, "hardening", "hardening_result", default=None)

    st.markdown(f"**Prompt:** {display_value(prompt_value)}")
    st.subheader("Experiment")
    if isinstance(experiment, dict):
        c1, c2 = st.columns(2)
        c1.metric("Target", display_value(pick(experiment, "target", "service")))
        c2.metric("Fault", display_value(pick(experiment, "fault", "type")))
        st.markdown(f"**Reason:** {display_value(pick(experiment, 'reason'))}")
        st.markdown(f"**Hypothesis:** {display_value(pick(experiment, 'hypothesis'))}")
    else:
        section_missing("Experiment details: Not run yet")

    st.subheader("Blind diagnosis")
    if isinstance(diagnosis, dict):
        suspect = pick(diagnosis, "suspect", "target", "service")
        confidence = pick(diagnosis, "confidence")
        correctness = pick(diagnosis, "correct", "is_correct")
        expected = pick(experiment, "target", "service") if isinstance(experiment, dict) else None
        if correctness is None and suspect is not None and expected is not None:
            correctness = str(suspect).lower() == str(expected).lower()
        badge = "<span class='ca-correct'>correct</span>" if correctness is True else (
            "<span class='ca-wrong'>wrong</span>" if correctness is False else ""
        )
        st.markdown(
            f"**Suspect:** {display_value(suspect)} &nbsp; **Confidence:** {display_value(confidence)} {badge}",
            unsafe_allow_html=True,
        )
    else:
        section_missing("Blind diagnosis: Not run yet")

    st.subheader("Remediation actions")
    if isinstance(remediation, list) and remediation:
        for action in remediation:
            st.markdown(f"- {display_value(action)}")
    elif isinstance(remediation, str) and remediation.strip():
        st.markdown(remediation)
    else:
        section_missing("Remediation actions: Not run yet")

    healed = pick(report, "healed_in_s", "healed_seconds", "healed_in")
    if healed is None and isinstance(hardening, dict):
        healed = pick(hardening, "healed_in_s", "healed_seconds")
    st.subheader("Recovery")
    if healed is None:
        section_missing("Healed in: Not run yet")
    else:
        st.success(f"Healed in {healed} s")

    st.subheader("Hardening result")
    if isinstance(hardening, dict):
        result = pick(hardening, "result", "status", "summary")
        st.write(display_value(result))
        llm_badges(report)
    else:
        section_missing("Hardening result: Not run yet")
        llm_badges(report)

st.divider()
st.header("Before vs after")
if report_error:
    st.error(f"Could not read data/heal_report.json: {report_error}")
else:
    hardening = pick(report, "hardening", "hardening_result", default=None) if isinstance(report, dict) else None
    before = pick(hardening, "before", "baseline", default={})
    after = pick(hardening, "after", "hardened", default={})
    before_score = pick(hardening, "resilience_score_before", default=pick(before, "resilience_score", "score"))
    after_score = pick(hardening, "resilience_score_after", default=pick(after, "resilience_score", "score"))
    metrics = ["error_rate", "p95_ms", "recovery_s", "data_survived"]
    rows = []
    chart_rows = []
    for metric in metrics:
        old = pick(before, metric)
        new = pick(after, metric)
        if old is not None or new is not None:
            rows.append({"Metric": metric, "Before": old, "After": new})
            chart_before = int(old) if isinstance(old, bool) else old
            chart_after = int(new) if isinstance(new, bool) else new
            if isinstance(chart_before, (int, float)) and isinstance(chart_after, (int, float)):
                chart_rows.append({"Metric": metric, "Before": chart_before, "After": chart_after})
    if before_score is not None or after_score is not None:
        rows.append({"Metric": "resilience_score", "Before": before_score, "After": after_score})
        if isinstance(before_score, (int, float)) and isinstance(after_score, (int, float)):
            chart_rows.append({"Metric": "resilience_score", "Before": before_score, "After": after_score})
    if rows:
        table = pd.DataFrame(rows)
        def highlight_data_survived(row: pd.Series) -> list[str]:
            if row.get("Metric") == "data_survived" and row.get("Before") is False and row.get("After") is True:
                return ["background-color: #173c2b; color: #a8efb5"] * len(row)
            return [""] * len(row)
        st.dataframe(table.style.apply(highlight_data_survived, axis=1), hide_index=True, use_container_width=True)
        if chart_rows:
            st.bar_chart(pd.DataFrame(chart_rows).set_index("Metric"))
        else:
            section_missing("Comparable numeric measurements: Not run yet")
    else:
        section_missing("Not run yet")

st.divider()
st.header("AI code review")
if review_error:
    st.error(f"Could not read data/code_review.json: {review_error}")
elif isinstance(code_review, dict) and isinstance(code_review.get("risks"), list) and code_review["risks"]:
    rows = []
    for risk in code_review["risks"]:
        if not isinstance(risk, dict):
            continue
        rows.append({
            "Service": pick(risk, "service", default="Not run yet"),
            "File:line": f"{pick(risk, 'file', default='?')}:{pick(risk, 'line', default='?')}",
            "Quote": pick(risk, "quote", default="Not run yet"),
            "Weakness": pick(risk, "weakness", default="Not run yet"),
            "Fix": pick(risk, "fix", default="Not run yet"),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        section_missing()
else:
    section_missing()

st.divider()
st.header("Dependency map")
if depmap_error:
    st.error(f"Could not read data/depmap.json: {depmap_error}")
elif isinstance(depmap, dict):
    services = depmap.get("services")
    edges = depmap.get("edges")
    if isinstance(services, list) and services:
        service_rows = []
        for service in services:
            if isinstance(service, dict):
                evidence = service.get("evidence", [])
                service_rows.append({
                    "Service": pick(service, "name", default="Not run yet"),
                    "Kind": pick(service, "kind", default="Not run yet"),
                    "Depends on": ", ".join(map(str, service.get("depends_on", []))) if isinstance(service.get("depends_on", []), list) else display_value(service.get("depends_on")),
                    "Evidence": "; ".join(map(str, evidence)) if isinstance(evidence, list) else display_value(evidence),
                })
        if service_rows:
            st.caption(f"Application: {display_value(depmap.get('app'))}")
            st.dataframe(pd.DataFrame(service_rows), hide_index=True, use_container_width=True)
        else:
            section_missing("Services: Not run yet")
    else:
        section_missing("Services: Not run yet")
    if isinstance(edges, list) and edges:
        edge_rows = [{
            "Source": pick(edge, "src", default="Not run yet"),
            "Destination": pick(edge, "dst", default="Not run yet"),
            "Type": pick(edge, "type", default="Not run yet"),
            "Evidence": display_value(edge.get("evidence")) if isinstance(edge, dict) else "Not run yet",
        } for edge in edges if isinstance(edge, dict)]
        if edge_rows:
            st.subheader("Dependency edges")
            st.dataframe(pd.DataFrame(edge_rows), hide_index=True, use_container_width=True)
        else:
            section_missing("Edges: Not run yet")
    else:
        section_missing("Edges: Not run yet")
else:
    section_missing()

st.divider()
st.header("Graph path")
path_value = pick(report, "graph_path", "node_path", "path") if isinstance(report, dict) else None
if isinstance(path_value, list) and path_value:
    chips = " <span class='ca-arrow'>→</span> ".join(
        f"<span class='ca-chip'>{html.escape(str(node))}</span>" for node in path_value
    )
    st.markdown(chips, unsafe_allow_html=True)
else:
    section_missing()\n