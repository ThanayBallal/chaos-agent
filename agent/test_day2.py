import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import analyst, code_review, patcher, planner

DEP = {"app": "robot-shop",
       "services": [{"name": "web", "kind": "proxy", "depends_on": ["cart", "catalogue", "user"]},
                    {"name": "cart", "kind": "app", "depends_on": ["redis", "catalogue"]},
                    {"name": "catalogue", "kind": "app", "depends_on": ["mongodb"]},
                    {"name": "user", "kind": "app", "depends_on": ["mongodb", "redis"]},
                    {"name": "payment", "kind": "app", "depends_on": ["user", "rabbitmq"]},
                    {"name": "redis", "kind": "cache", "depends_on": []},
                    {"name": "mongodb", "kind": "db", "depends_on": []},
                    {"name": "rabbitmq", "kind": "queue", "depends_on": []}],
       "edges": [{"src": "web", "dst": "cart", "type": "http", "evidence": "t"},
                 {"src": "web", "dst": "catalogue", "type": "http", "evidence": "t"},
                 {"src": "web", "dst": "user", "type": "http", "evidence": "t"},
                 {"src": "cart", "dst": "redis", "type": "cache", "evidence": "t"},
                 {"src": "cart", "dst": "catalogue", "type": "http", "evidence": "t"},
                 {"src": "catalogue", "dst": "mongodb", "type": "db", "evidence": "t"},
                 {"src": "user", "dst": "mongodb", "type": "db", "evidence": "t"},
                 {"src": "user", "dst": "redis", "type": "cache", "evidence": "t"},
                 {"src": "payment", "dst": "user", "type": "http", "evidence": "t"},
                 {"src": "payment", "dst": "rabbitmq", "type": "queue", "evidence": "t"}]}
BAS = {"error_rate": 0.004, "p95_ms": 180,
       "health": {svc["name"]: "healthy" for svc in DEP["services"]}}
RISK_STATE = {"service": "cart", "dependency": "redis", "file": "cart/server.js", "line": 379,
              "quote": "redisClient.setex(id, 3600, JSON.stringify(cart)", "fault": "kill",
              "weakness": "cart keeps its only copy of user state in redis, which has no volume or persistence",
              "fix": "give redis a volume with AOF persistence", "weight": 3}
RISK_FLAG = {"service": "catalogue", "dependency": "mongodb", "file": "catalogue/server.js", "line": 173,
             "quote": "mongoConnected = true;", "fault": "kill",
             "weakness": "mongoConnected is set true once and never reset", "fix": "reset the flag on close", "weight": 3}
RISK_TMO = {"service": "payment", "dependency": "user", "file": "payment/payment.py", "line": 64,
            "quote": "req = requests.get('http://{user}:8080/check/{id}'", "fault": "pause",
            "weakness": "HTTP call to user has no timeout, so a slow user blocks the worker indefinitely",
            "fix": "pass timeout=(connect, read) to every requests call", "weight": 2}


def exp(eid, tgt, fal):
    prm = dict(planner.PRM[fal])
    return {"id": eid, "target": tgt, "fault": fal, "params": prm, "risk": "high", "rank": int(eid[1:]),
            "reason": "r", "hypothesis": "h"}


def finding(rank, eid, tgt, weak="w", sev="high"):
    return {"rank": rank, "severity": sev, "target": tgt, "experiment": eid, "weakness": weak,
            "evidence": "error_rate 0.004 -> 0.62", "hypothesis_matched": None, "fix": "f"}


def result(eid, tgt, fal, err=0.02, **kw):
    return {"id": eid, "target": tgt, "fault": fal, "started_at": "T", "ended_at": "T", "error_rate": err,
            "p95_ms": 200, "recovery_s": 5, "aborted": False, "abort_reason": None, "observations": [], **kw}


class Isolated(unittest.TestCase):
    """No LLM, no writes into data/, a clean code-review risk table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.pat = [mock.patch.dict(os.environ, {"CHAOS_NO_LLM": "1", "CHAOS_NO_CODE": "1"}),
                    mock.patch.object(patcher, "OUT", tmp / "patch.json"),
                    mock.patch.object(code_review, "OUT", tmp / "missing_review.json"),
                    mock.patch.dict(code_review.LAST, clear=True)]
        for pat in self.pat:
            pat.start()

    def tearDown(self):
        for pat in reversed(self.pat):
            pat.stop()
        self.tmp.cleanup()


RISK_PUB = {"service": "payment", "dependency": "rabbitmq", "file": "payment/payment.py", "line": 120,
            "quote": "publisher.publish(order)", "fault": "kill",
            "weakness": "the order is published before the cart delete that can still fail",
            "fix": "publish only after the cart delete succeeds", "weight": 2}


def evd(rec="recovery 14s", loss=False):
    return f"error_rate 0.004 -> 0.62, p95 180 -> 2400ms, {rec}" + (", data_survived false" if loss else "")


class Patcher(Isolated):
    def make(self, fnd, pln):
        return patcher.make({"findings": fnd}, pln, DEP)

    def test_redis_state_loss_gives_redis_aof(self):
        code_review.LAST["e1"] = [RISK_STATE]
        pch = self.make([dict(finding(1, "e1", "redis"), evidence=evd("recovery 4s"))], [exp("e1", "redis", "kill")])
        self.assertEqual([(itm["id"], itm["fix"], itm["service"], itm["experiment"]) for itm in pch["patches"]],
                         [("p1", "redis_aof", "redis", "e1")])
        self.assertTrue(pch["patches"][0]["reason"].startswith("cart/server.js:379"))
        self.assertEqual(set(pch["patches"][0]), {"id", "experiment", "service", "fix", "reason", "expected"})
        self.assertEqual(pch["code_suggestions"], [])  # the state-loss weakness is covered by the live patch
        self.assertEqual(json.loads(patcher.OUT.read_text()), pch)

    def test_data_survived_false_on_redis_gives_redis_aof_without_code_review(self):
        fnd = [dict(finding(1, "e1", "redis", "Data loss: carts did not survive"), evidence=evd("recovery 4s", True))]
        pch = self.make(fnd, [exp("e1", "redis", "kill")])
        self.assertEqual([itm["fix"] for itm in pch["patches"]], ["redis_aof"])

    def test_killed_dependency_with_slow_or_no_recovery_gets_restart_policy(self):
        pln = [exp("e1", "mongodb", "kill"), exp("e2", "rabbitmq", "kill"), exp("e3", "catalogue", "kill"),
               exp("e4", "user", "pause")]
        fnd = [dict(finding(1, "e1", "mongodb"), evidence=evd("recovery 22s")),
               dict(finding(2, "e2", "rabbitmq"), evidence=evd("no recovery measured")),
               dict(finding(3, "e3", "catalogue"), evidence=evd("recovery 4s")),
               dict(finding(4, "e4", "user"), evidence=evd("no recovery measured"))]
        pch = self.make(fnd, pln)
        self.assertEqual([(itm["fix"], itm["service"]) for itm in pch["patches"]],
                         [("restart_policy", "mongodb"), ("restart_policy", "rabbitmq")])
        self.assertIn("22s", pch["patches"][0]["reason"])

    def test_code_weaknesses_become_code_suggestions(self):
        code_review.LAST.update(e1=[RISK_FLAG], e2=[RISK_TMO], e3=[RISK_PUB])
        pln = [exp("e1", "mongodb", "kill"), exp("e2", "user", "pause"), exp("e3", "rabbitmq", "kill")]
        fnd = [dict(finding(idx, f"e{idx}", tgt), evidence=evd("recovery 4s"))
               for idx, tgt in ((1, "mongodb"), (2, "user"), (3, "rabbitmq"))]
        pch = self.make(fnd, pln)
        self.assertEqual(pch["patches"], [])
        self.assertEqual({(itm["file"], itm["line"]) for itm in pch["code_suggestions"]},
                         {("catalogue/server.js", 173), ("payment/payment.py", 64), ("payment/payment.py", 120)})
        for itm in pch["code_suggestions"]:
            self.assertEqual(set(itm), {"file", "line", "suggestion"})

    def test_llm_wording_kept_and_invalid_items_dropped(self):
        code_review.LAST["e1"] = [RISK_STATE, RISK_FLAG]
        fnd = [dict(finding(1, "e1", "redis"), evidence=evd("no recovery measured"))]
        rep = {"patches": [
            {"experiment": "e1", "service": "redis", "fix": "redis_aof", "reason": "LLM reason", "expected": "LLM exp"},
            {"experiment": "e1", "service": "redis", "fix": "add_replica", "reason": "r", "expected": "e"},
            {"experiment": "e1", "service": "nginx", "fix": "restart_policy", "reason": "r", "expected": "e"},
            {"experiment": "e1", "service": "mongodb", "fix": "restart_policy", "reason": "r", "expected": "e"},
            {"experiment": "e1", "service": "mongodb", "fix": "redis_aof", "reason": "r", "expected": "e"}],
            "code_suggestions": [
            {"file": "catalogue/server.js", "line": 173, "suggestion": "LLM: reset mongoConnected on close"},
            {"file": "cart/server.js", "line": 1, "suggestion": "uncited line"}]}
        fake = lambda sp, up, schema=None, check=None, **kw: check(rep)
        with mock.patch.object(patcher.llm, "ask_json", fake):
            pch = self.make(fnd, [exp("e1", "redis", "kill")])
        got = {(itm["fix"], itm["service"]): itm for itm in pch["patches"]}
        self.assertEqual(set(got), {("redis_aof", "redis"), ("restart_policy", "redis")})  # only rule-eligible ones
        self.assertEqual(got[("redis_aof", "redis")]["reason"], "LLM reason")
        self.assertNotEqual(got[("restart_policy", "redis")]["reason"], "r")  # filled from the template
        self.assertEqual([(itm["file"], itm["line"], itm["suggestion"]) for itm in pch["code_suggestions"]],
                         [("catalogue/server.js", 173, "LLM: reset mongoConnected on close")])

    def test_every_patch_uses_the_fix_allowlist_and_depmap_services(self):
        code_review.LAST.update(e1=[RISK_STATE], e2=[RISK_FLAG])
        fnd = [dict(finding(1, "e1", "redis"), evidence=evd("no recovery measured", True)),
               dict(finding(2, "e2", "mongodb"), evidence=evd("recovery 30s"))]
        pch = self.make(fnd, [exp("e1", "redis", "kill"), exp("e2", "mongodb", "kill")])
        nam = {svc["name"] for svc in DEP["services"]}
        self.assertTrue(pch["patches"])
        for itm in pch["patches"]:
            self.assertIn(itm["fix"], patcher.FIXES)
            self.assertIn(itm["service"], nam)
        self.assertNotIn("override", pch)
        self.assertEqual([itm["id"] for itm in pch["patches"]], [f"p{idx}" for idx in range(1, len(pch["patches"]) + 1)])


class Planner(Isolated):
    def test_fallback_plan_covers_three_fault_types(self):
        pln = planner.plan(DEP, BAS, 3)
        self.assertEqual(len(pln), 3)
        self.assertEqual({exp["fault"] for exp in pln}, set(planner.CORE))
        self.assertEqual([(exp["id"], exp["rank"]) for exp in pln], [("e1", 1), ("e2", 2), ("e3", 3)])

    def test_latency_only_after_core_faults(self):
        pln = [exp("e1", "mongodb", "latency"), exp("e2", "redis", "kill"), exp("e3", "rabbitmq", "pause")]
        out = planner.diversify(pln, DEP, BAS, 3)
        self.assertEqual({exp["fault"] for exp in out}, set(planner.CORE))
        out = planner.diversify([exp("e1", "mongodb", "latency"), exp("e2", "redis", "kill"),
                                 exp("e3", "rabbitmq", "pause")], DEP, BAS, 4)
        self.assertEqual([exp["fault"] for exp in out][3], "latency")
        self.assertEqual({exp["fault"] for exp in out[:3]}, set(planner.CORE))

    def test_extra_target_is_new_healthy_and_fits_the_fault(self):
        pln = [exp("e1", "redis", "kill"), exp("e2", "mongodb", "pause"), exp("e3", "rabbitmq", "kill")]
        out = planner.diversify(pln, DEP, dict(BAS, health=dict(BAS["health"], catalogue="unhealthy")), 3)
        new = out[-1]
        self.assertEqual(new["fault"], "cpu_stress")
        self.assertEqual(new["target"], "user")  # highest-centrality healthy app not already planned (web, payment)
        self.assertEqual([exp["target"] for exp in out[:2]], ["redis", "mongodb"])  # code picks keep their rank

    def test_code_risks_follow_renumbered_experiments(self):
        pln = [exp("e1", "mongodb", "latency"), exp("e2", "redis", "kill"), exp("e3", "rabbitmq", "pause")]
        code_review.LAST.update(e2=[RISK_STATE], e3=[RISK_TMO])
        out = planner.diversify(pln, DEP, BAS, 3)
        byt = {exp["target"]: exp["id"] for exp in out}
        self.assertEqual(code_review.risks_for(byt["redis"]), [RISK_STATE])
        self.assertEqual(code_review.risks_for(byt["rabbitmq"]), [RISK_TMO])

    def test_small_plans_are_left_alone(self):
        pln = [exp("e1", "redis", "kill"), exp("e2", "mongodb", "kill")]
        self.assertEqual([exp["fault"] for exp in planner.diversify(pln, DEP, BAS, 2)], ["kill", "kill"])


class DataLoss(Isolated):
    def test_fallback_raises_severity_and_mentions_data_loss(self):
        res = result("e1", "redis", "kill", err=0.01, data_survived=False)
        itm = analyst.analyze(DEP, BAS, [res], [exp("e1", "redis", "kill")])["findings"][0]
        self.assertEqual(itm["severity"], "high")
        self.assertRegex(itm["weakness"].lower(), "data loss")
        self.assertIn("data_survived false", itm["evidence"])
        self.assertTrue(patcher.OUT.exists())

    def test_llm_cannot_downgrade_data_loss(self):
        res = result("e1", "redis", "kill", err=0.01, data_survived=False)
        rep = {"e1": {"experiment": "e1", "weakness": "redis restarted cleanly", "hypothesis": "matched",
                      "severity": "low", "fix": "restart (error_rate 0.01)"}}
        with mock.patch.object(analyst.llm, "ask_json", lambda *a, **kw: rep):
            itm = analyst.analyze(DEP, BAS, [res], [exp("e1", "redis", "kill")])["findings"][0]
        self.assertEqual(itm["severity"], "high")
        self.assertTrue(itm["weakness"].startswith("Data loss:"))

    def test_critical_stays_critical_and_survivors_unchanged(self):
        crt = result("e1", "redis", "kill", err=0.7, data_survived=False)
        oky = result("e2", "mongodb", "pause", err=0.01, data_survived=True)
        fnd = analyst.analyze(DEP, BAS, [crt, oky], [exp("e1", "redis", "kill"), exp("e2", "mongodb", "pause")])
        sev = {itm["experiment"]: itm for itm in fnd["findings"]}
        self.assertEqual(sev["e1"]["severity"], "critical")
        self.assertEqual(sev["e2"]["severity"], "low")
        self.assertNotRegex(sev["e2"]["weakness"].lower(), "data loss")


if __name__ == "__main__":
    unittest.main()
