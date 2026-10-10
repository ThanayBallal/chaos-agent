"""P3: offline safety tests for the runner. No Docker, no real faults.
A fake robot-shop serves catalogue / cart add / login; the fake fault makes cart add fail until reverted.
Run: python -m unittest monitor.test_monitor -v"""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from monitor import baseline, injector, runner
from safety import guard

RES_KEYS = {"id", "target", "fault", "started_at", "ended_at", "error_rate", "p95_ms", "recovery_s",
            "aborted", "abort_reason", "observations"}
BAS = {"window_s": 2, "requests": 100, "error_rate": 0.0, "p50_ms": 5, "p95_ms": 10,
       "health": {"redis": "healthy", "cart": "healthy"}, "captured_at": "TEST"}
EXP = {"id": "e1", "target": "redis", "fault": "kill", "params": {"duration_s": 3}}
SHOP = {"redis": "healthy", "cart": "healthy", "hang": False}


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *arg):
        pass

    def _send(self, code, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        except ConnectionError:  # BrokenPipe, ConnectionReset, ConnectionAborted (Windows)
            pass  # client already timed out

    def do_GET(self):
        if self.path.startswith("/api/cart/add"):
            if SHOP["hang"] and SHOP["redis"] != "healthy":
                time.sleep(3)  # like real cart with redis gone: no answer
            return self._send(200 if SHOP["redis"] == "healthy" else 500, {})
        self._send(200, [{"sku": "Watson"}])

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send(200, {})


class Fault:
    """Fake injector: 'kills' redis in the fake shop and counts reverts."""

    def __init__(self, fail=False, on_inject=None):
        self.fail, self.on_inject, self.injected, self.reverts = fail, on_inject, 0, 0

    def __call__(self, exp):
        if self.fail:
            raise RuntimeError("docker said no")
        self.injected += 1
        SHOP["redis"] = "unhealthy"
        if self.on_inject:
            self.on_inject()

        def rev():
            self.reverts += 1
            SHOP["redis"] = "healthy"
        return injector._once(rev)


class NewGuard:
    """Stand-in for P5's newer guard API: reset / ready / register(fnc, info) / unregister."""

    def __init__(self, not_ready=None):
        self.not_ready, self.calls, self.reg = not_ready, [], []

    def reset(self):
        self.calls.append("reset")

    def ready(self, smp, bas):
        self.calls.append("ready")
        return self.not_ready

    def register(self, fnc, info=None):
        self.calls.append("register")
        self.reg.append((fnc, info))

    def unregister(self, fnc):
        self.calls.append("unregister")

    def check(self, smp, bas, tgt=None, allowed=None):
        return None

    def kill_switch(self):
        return False


class AbortAt2s:
    """Old-style guard that aborts at t>=2."""

    def __init__(self):
        self.reg = []

    def register(self, fnc):
        self.reg.append(fnc)

    def check(self, smp, bas, tgt=None):
        return "test abort" if smp["t"] >= 2 else None

    def kill_switch(self):
        return False


class RunnerSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url, cls.hlth, cls.inj = baseline.URL, baseline.health, injector.inject
        cls.out, cls.rmax = runner.OUT, runner.REC_MAX
        baseline.URL = f"http://127.0.0.1:{cls.srv.server_port}"
        baseline.health = lambda: {svc: SHOP[svc] for svc in ("redis", "cart")}
        cls.tmp = tempfile.TemporaryDirectory()
        runner.OUT = Path(cls.tmp.name) / "results" / "result.json"
        runner.REC_MAX = 10

    @classmethod
    def tearDownClass(cls):
        baseline.stop()
        cls.srv.shutdown()
        baseline.URL, baseline.health, injector.inject = cls.url, cls.hlth, cls.inj
        runner.OUT, runner.REC_MAX = cls.out, cls.rmax
        cls.tmp.cleanup()

    def setUp(self):
        SHOP.update(redis="healthy", cart="healthy", hang=False)
        guard.STP.unlink(missing_ok=True)
        guard.revert_all()
        guard.reset() if hasattr(guard, "reset") else guard._brc.update(since=None)

    def tearDown(self):
        guard.STP.unlink(missing_ok=True)
        guard.revert_all()

    def _contract(self, res):
        self.assertEqual(set(res), RES_KEYS)
        self.assertEqual(json.loads(runner.OUT.read_text()), res)

    def test_timeout_reverts_and_measures(self):
        flt = Fault()
        injector.inject = flt
        res = runner.run(EXP, BAS, guard)
        self._contract(res)
        self.assertEqual(flt.reverts, 1)
        self.assertFalse(res["aborted"])
        self.assertGreater(res["error_rate"], 0)
        self.assertIsNotNone(res["recovery_s"])
        self.assertTrue(any(o.startswith("cart_add:") and "first error" in o for o in res["observations"]))
        self.assertTrue(any(o.startswith("catalogue:") and "no errors" in o for o in res["observations"]))

    def test_guard_abort_reverts_early(self):
        flt, grd = Fault(), AbortAt2s()
        injector.inject = flt
        res = runner.run({**EXP, "params": {"duration_s": 30}}, BAS, grd)
        self._contract(res)
        self.assertEqual(flt.reverts, 1)
        self.assertEqual(len(grd.reg), 1)
        self.assertTrue(res["aborted"])
        self.assertEqual(res["abort_reason"], "test abort")

    def test_kill_switch_reverts(self):
        flt = Fault(on_inject=lambda: guard.STP.touch())
        injector.inject = flt
        res = runner.run({**EXP, "params": {"duration_s": 30}}, BAS, guard)
        self._contract(res)
        self.assertEqual(flt.reverts, 1)
        self.assertTrue(res["aborted"])
        self.assertIn("kill switch", res["abort_reason"])
        self.assertIsNone(res["recovery_s"])
        self.assertIn("recovery not measured: kill switch", res["observations"])

    def test_injection_failure_is_reported(self):
        injector.inject = Fault(fail=True)
        res = runner.run(EXP, BAS, guard)
        self._contract(res)
        self.assertTrue(res["aborted"])
        self.assertIn("injection failed", res["abort_reason"])
        self.assertIsNone(res["error_rate"])

    def test_not_steady_skips_injection(self):
        flt, grd = Fault(), NewGuard(not_ready="not steady: unhealthy cart")
        injector.inject = flt
        res = runner.run(EXP, BAS, grd)
        self._contract(res)
        self.assertEqual(flt.injected, 0)
        self.assertIn("not injected", res["abort_reason"])
        self.assertNotIn("register", grd.calls)

    def test_new_guard_api_used(self):
        flt, grd = Fault(), NewGuard()
        injector.inject = flt
        res = runner.run({**EXP, "params": {"duration_s": 2}}, BAS, grd)
        self._contract(res)
        self.assertEqual(grd.calls[:3], ["reset", "ready", "register"])
        self.assertIn("unregister", grd.calls)
        self.assertEqual(grd.reg[0][1]["target"], "redis")
        self.assertEqual(flt.reverts, 1)

    def test_hanging_endpoint_keeps_1s_cadence(self):
        """Real kill-redis showed cart requests hanging; sampling must stay at ~1 s and other endpoints keep traffic."""
        tmo, baseline.TMO = baseline.TMO, 1.5
        SHOP["hang"] = True
        ts = []

        class Rec(AbortAt2s):
            def check(self, smp, bas, tgt=None):
                ts.append(smp["t"])
                return None

        injector.inject = Fault()
        try:
            res = runner.run({**EXP, "params": {"duration_s": 4}}, BAS, Rec())
        finally:
            baseline.TMO = tmo
        self._contract(res)
        print("\n" + "\n".join(res["observations"]))
        self.assertEqual(ts, [1, 2, 3, 4])
        obs = {o.split(":")[0]: o for o in res["observations"]}
        self.assertIn("timed out", obs["cart_add"])
        cart = int(obs["cart_add"].split(":")[1].split("/")[0])
        self.assertGreaterEqual(cart, 15)  # fixed rate: cart keeps being clicked while it hangs
        self.assertGreater(res["error_rate"], 0.15)  # true failure share, not diluted by fewer cart requests
        self.assertIn("no errors", obs["catalogue"])
        self.assertGreater(int(obs["catalogue"].split("/")[1].split()[0]), 20)

    def test_abort_counts_final_outcome_of_waiting_requests(self):
        """Real run 3: guard aborted while all cart requests were still waiting; result said error_rate 0.0."""
        tmo, baseline.TMO = baseline.TMO, 1.5
        SHOP["hang"] = True
        injector.inject = Fault()
        try:
            res = runner.run({**EXP, "params": {"duration_s": 30}}, BAS, AbortAt2s())
        finally:
            baseline.TMO = tmo
        self._contract(res)
        self.assertTrue(res["aborted"])
        obs = {o.split(":")[0]: o for o in res["observations"]}
        self.assertNotIn("no outcome", obs["cart_add"])
        self.assertIn("timed out", obs["cart_add"])
        self.assertGreater(res["error_rate"], 0.2)

    def test_revert_is_idempotent_with_guard(self):
        flt = Fault()
        injector.inject = flt
        runner.run({**EXP, "params": {"duration_s": 2}}, BAS, guard)
        guard.revert_all()
        self.assertEqual(flt.reverts, 1)


if __name__ == "__main__":
    unittest.main()
