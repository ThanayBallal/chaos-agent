# Contracts — READ BEFORE CODING

Every module reads/writes these exact shapes. Do not change a contract without telling the whole team.
Rule for every AI tool: **only edit files in your own folder, and follow this file.**

## depmap.json  (owner: P2 → used by P4, P5)
```json
{
  "app": "robot-shop",
  "services": [
    {"name": "cart", "kind": "app", "depends_on": ["redis", "catalogue"],
     "evidence": ["compose depends_on", "env REDIS_HOST=redis"]}
  ],
  "edges": [{"src": "cart", "dst": "redis", "type": "cache", "evidence": "env REDIS_HOST"}]
}
```
`kind` ∈ `app | db | cache | queue | proxy`. Every edge MUST have evidence.

## baseline.json  (owner: P3 → used by P4, P5)
```json
{"window_s": 120, "requests": 2400, "error_rate": 0.004, "p50_ms": 45, "p95_ms": 180,
 "health": {"cart": "healthy", "redis": "healthy"}, "captured_at": "2026-10-10T12:30:00+05:30"}
```

## plan.json  (owner: P4 → used by P5, P3)
```json
[{"id": "e1", "target": "redis", "fault": "kill", "params": {"duration_s": 45},
  "risk": "high", "rank": 1,
  "reason": "cart and session state depend on redis; single instance, no replica",
  "hypothesis": "cart requests fail while redis is down; recovers after restart"}]
```
`fault` ∈ `kill | pause | latency | cpu_stress` (see safety/allowlist.py).
`params`: kill/pause → `duration_s`; latency → `duration_s`, `delay_ms`; cpu_stress → `duration_s`, `cpus`.

## result.json  (owner: P3 → used by P4)
```json
{"id": "e1", "target": "redis", "fault": "kill", "started_at": "...", "ended_at": "...",
 "error_rate": 0.62, "p95_ms": 2400, "recovery_s": 14,
 "aborted": false, "abort_reason": null,
 "observations": ["cart 500s from t=3s", "catalogue unaffected"]}
```
`observations` are MEASURED facts only. Never copy the hypothesis here.

## findings.json  (owner: P4 → used by P5 / report / demo)
```json
{"resilience_score": 62,
 "findings": [{"rank": 1, "severity": "critical", "target": "redis", "experiment": "e1",
   "weakness": "single redis instance, cart has no fallback",
   "evidence": "error_rate 0.004 → 0.62, recovery 14s",
   "hypothesis_matched": true,
   "fix": "add restart: always + healthcheck; retry with backoff in cart; consider redis replica"}]}
```
`severity` ∈ `critical | high | medium | low`.

## Safety limits (owner: P5)
- Only containers in compose project `robot-shop` may be touched.
- Max fault duration 60 s. One experiment at a time.
- Abort if error_rate > 0.5 for 10 s, or p95 > 5000 ms, or a service **outside the target's blast radius** goes unhealthy.
  Blast radius = the target + every service that transitively depends on it (from depmap `depends_on` and `edges`).
- Kill switch: create file `STOP` in repo root (or Ctrl+C) → all faults reverted immediately.
  A stale `STOP` is removed when `guard.install()` runs at startup.
- Active faults are mirrored to `data/active_faults.json`; `python -m safety.recover` restores all robot-shop containers.

### Guard API (`safety/guard.py`, used by the runner)
- `approve(exp, dep, mock=False)` → `None` or rejection reason. Checks allowlist, target in depmap, params, duration ≤ 60 s,
  and (unless `mock`) that a Docker container with label `com.docker.compose.project=robot-shop` exists for the target.
  On success it remembers the target's blast radius for `check()`.
- `reset()` — clear breach timers. Call before each experiment.
- `ready(sample, bas)` → `None` or reason if not at steady state (any service unhealthy, or error_rate > 3× baseline).
  Call before injecting.
- `register(fnc, info=None)` — register a zero-arg revert function right after injecting; `info` is any JSON-able description (e.g. the exp).
- `unregister(fnc)` — call after a revert has run successfully.
- `check(sample, bas, tgt, allowed=None)` → `None` or abort reason. `sample` = `{"t", "error_rate", "p95_ms", "health": {svc: state}}`.
  `allowed` = services that may go unhealthy; defaults to the blast radius from the last `approve()` of `tgt`, else just `{tgt}`.
- `kill_switch()` → `True` if `STOP` exists. `revert_all()` — run every registered revert (also on Ctrl+C and at exit).

## Day 2 additions

### result.json: new field (P3)
"data_survived": true | false | null
# A cart is created before the fault and read back after recovery. null means it was not probed.
# Also add a matching measured observation line.

### patch.json (owner: P4 -> used by P1, P5)
{"patches": [{"id": "p1", "experiment": "e1", "service": "redis", "fix": "redis_aof",
   "reason": "cart/server.js:379 carts stored only in redis, no persistence",
   "expected": "carts survive a redis crash"}],
 "code_suggestions": [{"file": "cart/server.js", "line": 399, "suggestion": "set redisConnected=false on error"}]}
"fix" must be one of: redis_aof, restart_policy.
Fixes are applied live and reverted live. Robot Shop files are never edited.

### comparison.json (owner: P3 helper, called by P5)
{"patch_ids": ["p1"], "score": {"before": 50, "after": 78},
 "experiments": [{"id": "e1", "target": "redis", "fault": "kill", "improved": true,
   "error_rate": [0.62, 0.05], "p95_ms": [2400, 300], "recovery_s": [14, 4], "data_survived": [false, true]}]}
Each pair is [before, after].
"improved" = data_survived went false->true, OR error_rate dropped >= 0.10, OR recovery_s dropped >= 30%.
At most 2 fix attempts per run.

### Interfaces
- agent/patcher.py:   make(fnd, pln, dep) -> patch.json dict                        (P4)
- setup/patch.py:     apply(pat) -> None ; revert() -> None                         (P1)
- monitor/compare.py: compare(rsl_before, rsl_after, fnd_before, fnd_after) -> comparison.json dict  (P3)
- graph.py:           LangGraph flow map -> plan -> run -> analyze -> patch -> apply -> rerun -> compare  (P5)
