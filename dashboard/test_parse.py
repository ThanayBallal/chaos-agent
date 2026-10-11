from pathlib import Path

from dashboard.parse import Parser

LOG = Path(__file__).with_name("sample_campaign.log")


def run(lns):
    par = Parser()
    return [evt for lin in lns for evt in par.feed(lin)]


def kinds(evs, typ):
    return [evt for evt in evs if evt["t"] == typ]


def test_full_campaign_log():
    evs = run(LOG.read_text(encoding="utf-8").splitlines())
    wks = kinds(evs, "weakness")
    assert [wek["where"] for wek in wks] == ["catalogue/server.js:68", "user/server.js:86", "cart/server.js:85",
                                             "cart/server.js:379"]
    assert [cyc["fault"] for cyc in kinds(evs, "cycle")] == ["kill", "pause", "latency", "kill"]
    dxs = kinds(evs, "diagnosis")
    assert len(dxs) == 4 and all(dgn["correct"] for dgn in dxs)
    assert [hel["seconds"] for hel in kinds(evs, "healed")] == [38, 24, 31, 31]
    assert len(kinds(evs, "hardening")) == 2 and len(kinds(evs, "hardening_na")) == 2
    bfa = kinds(evs, "before_after")[-1]
    assert bfa["data_survived"] == [False, True] and bfa["score"] == [85, 98]
    srv = kinds(evs, "survived")[-1]
    assert srv["before"] is False and srv["reruns"] == [True]
    assert kinds(evs, "agents")[0]["agents"] == {"chaos_agent": "llm", "diagnosis_agent": "llm",
                                                 "remediation_agent": "llm"}
    rows = kinds(evs, "summary_row")
    assert [row["rank"] for row in rows] == [1, 2, 3, 4] and rows[3]["score"] == "85 -> 98"
    assert kinds(evs, "hold_wait") and kinds(evs, "hold_end")
    assert [inj["target"] for inj in kinds(evs, "injected")] == ["mongodb", "mongodb", "redis", "redis"]


def test_details_attach_to_context():
    evs = run([" 4. cart -> redis (kill)  cart/server.js:379", "    code: redisClient.setex(id)",
               "== Experiment: kill redis {\"duration_s\": 30}", "   hypothesis: carts are lost"])
    det = kinds(evs, "detail")
    assert det[0] == {"t": "detail", "of": "weakness", "key": "code", "value": "redisClient.setex(id)"}
    assert det[1]["of"] == "experiment" and det[1]["key"] == "hypothesis"


def test_chosen_and_metrics_and_nodes():
    evs = run(["[heal] chaos_agent: [fallback] kill redis {'duration_s': 15}: the prompt asked for: 'kill redis'",
               "[heal.ports] observe: error_rate=0.322 p95=6022ms unhealthy=['mongodb']",
               "[heal] verify: attempt 1: healed, recovery 21s"])
    cho = kinds(evs, "chosen")[0]
    assert cho["source"] == "fallback" and cho["params"] == {"duration_s": 15}
    met = kinds(evs, "metrics")[0]
    assert met["p95_ms"] == 6022 and met["unhealthy"] == ["mongodb"]
    assert [nod["node"] for nod in kinds(evs, "node")] == ["chaos_agent", "verify"]


def test_not_healed_and_fallback_agents():
    evs = run(["== Not healed: aborted (kill switch (STOP file))",
               "== Agents: chaos_agent=fallback (RuntimeError: x), diagnosis_agent=llm, remediation_agent=fallback+fallback (y)"])
    assert kinds(evs, "not_healed")[0]["why"].startswith("aborted")
    assert kinds(evs, "agents")[0]["agents"] == {"chaos_agent": "fallback", "diagnosis_agent": "llm",
                                                 "remediation_agent": "fallback"}
