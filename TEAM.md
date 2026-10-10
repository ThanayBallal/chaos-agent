# Team plan — who does what

Workflow: branch from `main` → work only in your folder → merge a WORKING piece every 1–2 hours (P5 merges).
`main` must always run. Use files in `data/` (mocks) until the real ones exist.

| Person | Branch | Folder | Tool |
|---|---|---|---|
| P1 App + Docker | `p1-setup` | `setup/` | Codex |
| P2 Dependency map | `p2-mapper` | `mapper/` | Codex |
| P3 Monitor + runner | `p3-monitor` | `monitor/` | Claude Code |
| P4 AI agent | `p4-agent` | `agent/` | Claude Code |
| P5 Safety + integration | `p5-safety` | `safety/`, `main.py`, README | Claude Code |

---

## P1 — setup/
**Done when:** `setup/start` brings robot-shop up healthy from scratch, `setup/reset` restores a clean state, `setup/health` prints pass/fail per service.

Prompt for your AI tool:
> Read contracts.md and TEAM.md. Only edit files in setup/. Robot Shop (github.com/instana/robot-shop) is cloned into ../robot-shop by setup/start. Finish start.ps1/start.sh, reset.ps1/reset.sh, and write health.py that checks every compose service is running (docker SDK), http://localhost:8080 returns 200, and prints a pass/fail table. Exit code 0 only if all pass.

## P2 — mapper/
**Done when:** `python -m mapper.mapper` writes `data/depmap.json` matching the contract, every edge has evidence, and it exports `data/depmap.mmd` (Mermaid).

Prompt:
> Read contracts.md. Only edit files in mapper/. Implement build_depmap(compose_path) that parses robot-shop's docker-compose.yaml: services, depends_on, networks, and environment variables that reference other service hostnames (e.g. REDIS_HOST, MONGO_URL, AMQP_HOST, CART_URL). Classify kind (db/cache/queue/proxy/app) from image names. Each edge needs an evidence string. Also write a Mermaid graph. Compare against data/mock_depmap.json and note differences.

## P3 — monitor/
**Done when:** `baseline.capture()` writes real baseline.json under load; `runner.run(exp, ...)` injects a fault via injector.py, samples metrics every second, calls the guard, reverts, measures recovery, writes result.json.

Prompt:
> Read contracts.md. Only edit files in monitor/. Implement baseline.py (drive traffic at localhost:8080 — catalogue, cart add, login — record error rate, p50/p95, health), injector.py (kill/pause/latency/cpu_stress on a compose service via docker SDK; latency via a pumba container or tc; every inject returns a revert function), runner.py (inject → sample each 1s → ask guard.check() → revert → measure recovery_s until error rate returns near baseline). Observations must be measured facts only.

## P4 — agent/
**Done when:** `planner.plan(dep, bas)` returns a ranked plan.json-shaped list with reasons; `analyst.analyze(dep, bas, results)` returns findings.json with severity, evidence and fixes. Works against data/mock_*.json first.

Prompt:
> Read contracts.md. Only edit files in agent/. Implement llm.py (one function ask_json(system, user) returning parsed JSON, with retry on invalid JSON). planner.py: compute a centrality score per service from depmap (how many services transitively depend on it), send depmap + baseline + scores to the LLM, require JSON matching plan.json, validate fault types against safety/allowlist.py, fall back to a deterministic ranking if the LLM fails. analyst.py: given results, rank weaknesses by severity using the measured numbers, propose concrete fixes (compose settings, retries, timeouts, healthchecks, replicas), mark hypothesis_matched, compute resilience_score 0–100.

## P5 — safety/ + main.py + README
**Done when:** guard rejects anything outside the allowlist/project, aborts on threshold breach, STOP file reverts everything; main.py runs the full pipeline end to end on mocks, then on real modules.

Prompt:
> Read contracts.md. Only edit safety/, main.py, README.md. Implement guard.py: approve(exp, dep) validates fault, target exists and is in project robot-shop, duration ≤ 60s; check(sample, bas) returns an abort reason or None per the limits in contracts.md; kill_switch() true if STOP file exists. Register a Ctrl+C handler that calls all pending revert functions. Keep main.py working on data/mock_*.json at all times.

---

## Day-1 checkpoint demo (target)
App running → depmap shown → one fault (kill redis) injected → impact visible vs baseline → guard auto-revert / STOP tested once.
