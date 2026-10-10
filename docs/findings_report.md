# Robot Shop resilience findings

## Summary

| Application | Resilience score | Services mapped | Dependencies mapped |
|---|---:|---:|---:|
| robot-shop | 50/100 | 12 | 19 |

## Ranked weaknesses

### 1. redis — CRITICAL

- **Experiment:** `e2`
- **Weakness:** kill on redis raised errors to 62%
- **Measured evidence:** error_rate 0.004 -> 0.62, p95 180 -> 2400ms, recovery 14s
- **Hypothesis matched:** Not evaluated
- **Source motivation:**
  - `cart/server.js:379` — cart keeps its only copy of user state in redis, which has no volume or persistence in docker-compose.yaml
  - `cart/server.js:399` — redisConnected is set true once and never reset, so /health keeps reporting redis as connected after it is lost
  - `cart/server.js:394` — the redis client error handler only logs; callers get no fallback or fast failure
  - `user/server.js:262` — the redis client error handler only logs; callers get no fallback or fast failure
- **Recommended fix:** add restart policy and healthcheck for redis; retries with backoff and timeouts in its dependents

### 2. mongodb — CRITICAL

- **Experiment:** `e1`
- **Weakness:** pause on mongodb raised errors to 55%
- **Measured evidence:** error_rate 0.004 -> 0.55, p95 180 -> 5200ms, recovery 22s, aborted: p95 > 5000 ms
- **Hypothesis matched:** Not evaluated
- **Source motivation:**
  - `catalogue/server.js:173` — mongoConnected is set true once and never reset, so /health keeps reporting mongodb as connected after it is lost
  - `user/server.js:288` — mongoConnected is set true once and never reset, so /health keeps reporting mongodb as connected after it is lost
  - `catalogue/server.js:177` — the mongodb retry loop only covers the first connect; later disconnects are left to the driver with no app-level handling
  - `user/server.js:292` — the mongodb retry loop only covers the first connect; later disconnects are left to the driver with no app-level handling
- **Recommended fix:** add restart policy and healthcheck for mongodb; retries with backoff and timeouts in its dependents

## Evidence boundaries

Measured impact is taken from `findings.json`; source motivations are taken from verified `code_review.json` citations. A missing citation or comparison is reported as unavailable rather than inferred. Before/after values appear only when `comparison.json` exists.
