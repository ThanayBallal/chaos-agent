import json

import pytest

from report import build


DEPMAP = {
    "app": "robot-shop",
    "services": [{"name": "cart", "kind": "app", "depends_on": ["redis"], "evidence": ["env"]},
                 {"name": "redis", "kind": "cache", "depends_on": [], "evidence": []}],
    "edges": [{"src": "cart", "dst": "redis", "type": "cache", "evidence": "env REDIS_HOST=redis"}],
}
FINDINGS = {
    "resilience_score": 50,
    "findings": [{"rank": 1, "severity": "critical", "target": "redis", "experiment": "e1",
                  "weakness": "cart state was lost", "evidence": "error_rate 0.62, recovery 14s",
                  "hypothesis_matched": True, "fix": "enable AOF persistence"}],
}
REVIEW = {
    "source": "static",
    "risks": [{"service": "cart", "dependency": "redis", "file": "cart/server.js", "line": 379,
               "weakness": "cart stores its only copy in Redis", "weight": 3}],
}
COMPARISON = {
    "patch_ids": ["p1"],
    "score": {"before": 50, "after": 78},
    "experiments": [{"id": "e1", "target": "redis", "fault": "kill", "improved": True,
                     "error_rate": [0.62, 0.05], "p95_ms": [2400, 300], "recovery_s": [14, 4],
                     "data_survived": [False, True]}],
}


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_build_includes_citation_and_before_after(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write(data / "findings.json", FINDINGS)
    _write(data / "code_review.json", REVIEW)
    _write(data / "comparison.json", COMPARISON)
    _write(data / "depmap.json", DEPMAP)

    output = build.build(data, tmp_path / "docs" / "findings_report.md")
    text = output.read_text(encoding="utf-8")

    assert "50/100" in text
    assert "`cart/server.js:379`" in text
    assert "62.0% → 5.0%" in text
    assert "2400 ms → 300 ms" in text
    assert "no → yes" in text


def test_optional_inputs_are_not_invented(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write(data / "findings.json", FINDINGS)
    _write(data / "depmap.json", DEPMAP)

    output = build.build(data, tmp_path / "report.md")
    text = output.read_text(encoding="utf-8")

    assert "No matching verified source citation" in text
    assert "## Before/after comparison" not in text


def test_depmap_is_required(tmp_path):
    _write(tmp_path / "findings.json", FINDINGS)
    with pytest.raises(FileNotFoundError, match="depmap.json"):
        build.build(tmp_path, tmp_path / "report.md")
