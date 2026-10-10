import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import code_review as cr
from agent import planner

DEP = {"app": "robot-shop",
       "services": [{"name": "cart", "kind": "app", "depends_on": ["redis"]},
                    {"name": "redis", "kind": "cache", "depends_on": []},
                    {"name": "payment", "kind": "app", "depends_on": ["cart"]}],
       "edges": [{"src": "cart", "dst": "redis", "type": "cache", "evidence": "env"},
                 {"src": "payment", "dst": "cart", "type": "http", "evidence": "src"}]}
BAS = {"error_rate": 0.0, "p95_ms": 50, "health": {"cart": "healthy", "redis": "healthy", "payment": "healthy"}}
CART = """var redisConnected = false;
app.get('/health', (req, res) => { res.json({redis: redisConnected}); });
redisClient.setex(id, 3600, JSON.stringify(cart), (err, data) => {});
redisClient.on('error', (e) => {
    logger.error('Redis ERROR', e);
});
redisClient.on('ready', (r) => {
    redisConnected = true;
});
"""
PAY = "req = requests.delete('http://{cart}:8080/cart/{id}'.format(cart=CART, id=id))\n"


class CodeReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app = Path(self.tmp.name)
        (app / "cart").mkdir()
        (app / "payment").mkdir()
        (app / "cart" / "server.js").write_text(CART)
        (app / "payment" / "payment.py").write_text(PAY)
        (app / "docker-compose.yaml").write_text("services:\n  redis:\n    image: redis\n")
        self.pat = [mock.patch.object(cr, "APP", app), mock.patch.object(cr, "OUT", app / "review.json")]
        for pat in self.pat:
            pat.start()

    def tearDown(self):
        for pat in self.pat:
            pat.stop()
        self.tmp.cleanup()

    def test_static_scan_cites_real_lines(self):
        _, src = cr.collect(DEP)
        rsk = cr.static_scan(DEP, src)
        got = {(itm["file"], itm["line"], itm["dependency"]) for itm in rsk}
        self.assertIn(("cart/server.js", 8, "redis"), got)  # flag set true, only reset in declaration
        self.assertIn(("cart/server.js", 3, "redis"), got)  # setex with no redis volume
        self.assertIn(("payment/payment.py", 1, "cart"), got)  # requests without timeout
        for itm in rsk:
            self.assertEqual(itm["quote"], src[itm["file"]][itm["line"] - 1].strip())

    def test_verify_rejects_hallucinated_citations(self):
        _, src = cr.collect(DEP)
        bad = {"risks": [{"service": "cart", "dependency": "redis", "file": "cart/server.js", "line": 8,
                          "quote": "redis.reconnect({retries: 0})", "weakness": "x", "fault": "kill",
                          "params": {"duration_s": 30}, "hypothesis": "h", "fix": "f"},
                         {"service": "cart", "dependency": "redis", "file": "cart/nope.js", "line": 1,
                          "quote": "redisConnected = true", "weakness": "x", "fault": "kill",
                          "params": {"duration_s": 30}, "hypothesis": "h", "fix": "f"}]}
        with self.assertRaises(ValueError):
            cr.verify(bad, DEP, src)

    def test_verify_keeps_real_citation_and_clamps_params(self):
        _, src = cr.collect(DEP)
        raw = {"risks": [{"service": "cart", "dependency": "redis", "file": "cart/server.js", "line": 9,
                          "quote": "redisConnected = true;", "weakness": "w", "fault": "kill",
                          "params": {"duration_s": 999}, "hypothesis": "h", "fix": "f"}]}
        out = cr.verify(raw, DEP, src)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["line"], 9)
        self.assertEqual(out[0]["params"], {"duration_s": 30})

    def test_plan_is_code_driven_and_guard_approved(self):
        with mock.patch.dict("os.environ", {"CHAOS_NO_LLM": "1"}):
            pln = planner.plan(DEP, BAS, 2)
        self.assertEqual([exp["id"] for exp in pln], ["e1", "e2"])
        self.assertTrue(pln[0]["reason"].startswith("code review:"))
        self.assertIn(pln[0]["target"], {"redis", "cart"})
        self.assertTrue(cr.risks_for(pln[0]["id"]))
        self.assertTrue(json.loads(cr.OUT.read_text())["risks"])

    def test_no_source_falls_back_to_graph_plan(self):
        with mock.patch.object(cr, "APP", Path(self.tmp.name) / "missing"), \
                mock.patch.dict("os.environ", {"CHAOS_NO_LLM": "1"}):
            pln = planner.plan(DEP, BAS, 2)
        self.assertTrue(pln)
        self.assertFalse(pln[0]["reason"].startswith("code review:"))


if __name__ == "__main__":
    unittest.main()
