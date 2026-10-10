"""P3: tests for monitor/compare.py against the contracts.md comparison.json shape and 'improved' rule.
Run: python -m unittest monitor.test_compare -v"""
import unittest

from monitor.compare import compare, improved


def res(eid="e1", tgt="redis", fal="kill", err=0.62, p95=2400, rec=14, srv=False):
    return {"id": eid, "target": tgt, "fault": fal, "error_rate": err, "p95_ms": p95,
            "recovery_s": rec, "data_survived": srv, "aborted": False, "abort_reason": None, "observations": []}


class Compare(unittest.TestCase):
    def test_contract_example(self):
        out = compare([res()], [res(err=0.05, p95=300, rec=4, srv=True)],
                      {"resilience_score": 50}, {"resilience_score": 78},
                      {"patches": [{"id": "p1", "experiment": "e1", "service": "redis", "fix": "redis_aof"}]})
        self.assertEqual(out, {"patch_ids": ["p1"], "score": {"before": 50, "after": 78},
                               "experiments": [{"id": "e1", "target": "redis", "fault": "kill", "improved": True,
                                                "error_rate": [0.62, 0.05], "p95_ms": [2400, 300],
                                                "recovery_s": [14, 4], "data_survived": [False, True]}]})

    def test_improved_rule(self):
        self.assertTrue(improved(res(srv=False), res(srv=True)))                      # data now survives
        self.assertTrue(improved(res(err=0.30, srv=None), res(err=0.20, srv=None)))   # exactly 0.10 drop
        self.assertFalse(improved(res(err=0.30, srv=None), res(err=0.21, srv=None)))  # 0.09 drop
        self.assertTrue(improved(res(rec=10, srv=None), res(rec=7, srv=None)))        # exactly 30% faster
        self.assertFalse(improved(res(rec=10, srv=None), res(rec=8, srv=None)))       # 20% faster
        self.assertFalse(improved(res(srv=True), res(srv=True)))                      # no change
        self.assertFalse(improved(res(srv=False), res(srv=None)))                     # not probed after: no claim
        self.assertFalse(improved(res(err=None, rec=None, srv=None), res(err=0.0, rec=1, srv=None)))  # never ran

    def test_matching_and_missing(self):
        bef = [res("e1"), res("e2", tgt="mongodb", fal="pause")]
        aft = [res("x9", tgt="mongodb", fal="pause", err=0.1)]  # different id, same target+fault
        out = compare(bef, aft, None, {"resilience_score": 60, "patch_ids": ["p2"]})
        self.assertEqual([exp["id"] for exp in out["experiments"]], ["e2"])  # e1 has no after-run: left out
        self.assertEqual(out["patch_ids"], ["p2"])
        self.assertEqual(out["score"], {"before": None, "after": 60})


if __name__ == "__main__":
    unittest.main()
