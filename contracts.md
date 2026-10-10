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
- Abort if error_rate > 0.5 for 10 s, or p95 > 5000 ms, or a non-target service goes unhealthy.
- Kill switch: create file `STOP` in repo root (or Ctrl+C) → all faults reverted immediately.
