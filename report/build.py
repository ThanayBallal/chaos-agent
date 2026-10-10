"""Build docs/findings_report.md from the pipeline's JSON artifacts.

Usage:
    python -m report.build
    python report/build.py --data-dir data --output docs/findings_report.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUTPUT = ROOT / "docs" / "findings_report.md"


def _load(path: Path, *, required: bool) -> dict[str, Any]:
    """Load one JSON object, returning an empty object for an absent optional file."""
    if not path.exists():
        if required:
            raise FileNotFoundError(f"required report input not found: {path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"report input must be a JSON object: {path}")
    return value


def _text(value: Any, default: str = "Not recorded") -> str:
    """Make contract data safe inside Markdown paragraphs and table cells."""
    if value is None or value == "":
        return default
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _matched(value: Any) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "Not evaluated"


def _pair(values: Any, formatter) -> str:
    if not isinstance(values, list) or len(values) != 2:
        return "Not recorded"
    return f"{formatter(values[0])} → {formatter(values[1])}"


def _percent(value: Any) -> str:
    return "—" if value is None else f"{float(value):.1%}"


def _milliseconds(value: Any) -> str:
    return "—" if value is None else f"{float(value):g} ms"


def _seconds(value: Any) -> str:
    return "—" if value is None else f"{float(value):g} s"


def _boolean(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "—"


def _citations(finding: dict[str, Any], review: dict[str, Any]) -> list[dict[str, Any]]:
    """Find verified review risks that could have motivated a finding's target."""
    risks = review.get("risks", [])
    if not isinstance(risks, list):
        return []
    target = finding.get("target")
    matched = [risk for risk in risks if isinstance(risk, dict) and risk.get("dependency") == target]
    if not matched:
        matched = [risk for risk in risks if isinstance(risk, dict) and risk.get("service") == target]
    return sorted(
        matched,
        key=lambda risk: (-int(risk.get("weight", 0)), str(risk.get("file", "")), int(risk.get("line", 0))),
    )


def _comparison_section(comparison: dict[str, Any]) -> list[str]:
    if not comparison:
        return []

    lines = ["## Before/after comparison", ""]
    score = comparison.get("score", {})
    if isinstance(score, dict) and score.get("before") is not None and score.get("after") is not None:
        lines.extend(
            [
                "| Score before | Score after | Change |",
                "|---:|---:|---:|",
                f"| {score['before']} | {score['after']} | {float(score['after']) - float(score['before']):+g} |",
                "",
            ]
        )

    patch_ids = comparison.get("patch_ids", [])
    if isinstance(patch_ids, list) and patch_ids:
        lines.extend([f"Applied patches: {', '.join(f'`{_text(item)}`' for item in patch_ids)}", ""])

    experiments = comparison.get("experiments", [])
    if not isinstance(experiments, list) or not experiments:
        lines.extend(["No per-experiment comparison was recorded.", ""])
        return lines

    lines.extend(
        [
            "| Experiment | Target | Fault | Error rate | p95 latency | Recovery | Data survived | Improved |",
            "|---|---|---|---:|---:|---:|---|---|",
        ]
    )
    for experiment in experiments:
        if not isinstance(experiment, dict):
            continue
        lines.append(
            "| {id} | {target} | {fault} | {error} | {p95} | {recovery} | {data} | {improved} |".format(
                id=_text(experiment.get("id")),
                target=_text(experiment.get("target")),
                fault=_text(experiment.get("fault")),
                error=_pair(experiment.get("error_rate"), _percent),
                p95=_pair(experiment.get("p95_ms"), _milliseconds),
                recovery=_pair(experiment.get("recovery_s"), _seconds),
                data=_pair(experiment.get("data_survived"), _boolean),
                improved=_boolean(experiment.get("improved")),
            )
        )
    lines.append("")
    return lines


def render(
    findings: dict[str, Any],
    review: dict[str, Any],
    comparison: dict[str, Any],
    depmap: dict[str, Any],
) -> str:
    """Render the supplied contract objects as a Markdown findings report."""
    services = depmap.get("services", [])
    edges = depmap.get("edges", [])
    ranked = findings.get("findings", [])
    if not isinstance(services, list) or not isinstance(edges, list):
        raise ValueError("depmap.json must contain 'services' and 'edges' lists")
    if not isinstance(ranked, list):
        raise ValueError("findings.json must contain a 'findings' list")

    lines = [
        "# Robot Shop resilience findings",
        "",
        "## Summary",
        "",
        "| Application | Resilience score | Services mapped | Dependencies mapped |",
        "|---|---:|---:|---:|",
        f"| {_text(depmap.get('app'), 'robot-shop')} | {_text(findings.get('resilience_score'))}/100 | "
        f"{len(services)} | {len(edges)} |",
        "",
        "## Ranked weaknesses",
        "",
    ]

    if not ranked:
        lines.extend(["No weaknesses were recorded.", ""])
    for finding in sorted(ranked, key=lambda item: int(item.get("rank", 10**9))):
        rank = finding.get("rank", "—")
        target = _text(finding.get("target"))
        severity = _text(finding.get("severity")).upper()
        lines.extend(
            [
                f"### {rank}. {target} — {severity}",
                "",
                f"- **Experiment:** `{_text(finding.get('experiment'))}`",
                f"- **Weakness:** {_text(finding.get('weakness'))}",
                f"- **Measured evidence:** {_text(finding.get('evidence'))}",
                f"- **Hypothesis matched:** {_matched(finding.get('hypothesis_matched'))}",
                "- **Source motivation:**",
            ]
        )
        citations = _citations(finding, review)
        if citations:
            for risk in citations[:5]:
                location = f"{_text(risk.get('file'))}:{_text(risk.get('line'))}"
                lines.append(f"  - `{location}` — {_text(risk.get('weakness'))}")
        else:
            lines.append("  - No matching verified source citation was present in `data/code_review.json`.")
        lines.extend([f"- **Recommended fix:** {_text(finding.get('fix'))}", ""])

    lines.extend(_comparison_section(comparison))
    lines.extend(
        [
            "## Evidence boundaries",
            "",
            "Measured impact is taken from `findings.json`; source motivations are taken from verified "
            "`code_review.json` citations. A missing citation or comparison is reported as unavailable rather "
            "than inferred. Before/after values appear only when `comparison.json` exists.",
            "",
        ]
    )
    return "\n".join(lines)


def build(data_dir: Path = DATA, output: Path = OUTPUT) -> Path:
    """Read pipeline artifacts, write the Markdown report, and return its path."""
    data_dir = Path(data_dir)
    output = Path(output)
    content = render(
        findings=_load(data_dir / "findings.json", required=True),
        review=_load(data_dir / "code_review.json", required=False),
        comparison=_load(data_dir / "comparison.json", required=False),
        depmap=_load(data_dir / "depmap.json", required=True),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    path = build(args.data_dir, args.output)
    print(f"[report] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
