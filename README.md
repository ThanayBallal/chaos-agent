# Chaos Agent — AI-driven chaos engineering for cloud-native resilience

An AI agent that maps a containerized application's dependencies, plans the most informative failure experiments, injects them safely with automatic rollback, measures the impact against a baseline, and reports ranked weaknesses with suggested fixes.

**System under test:** [Stan's Robot Shop](https://github.com/instana/robot-shop) (Apache-2.0), run locally with Docker Compose. It is cloned separately and is not part of this repository.

## Pipeline
```
map dependencies → capture baseline → AI plans experiments → safety check
→ inject fault → observe → auto-revert → AI analyzes → ranked findings + fixes
```

## Requirements
- Docker Desktop (WSL2 backend on Windows), ≥ 6 GB memory for Docker
- Python 3.10+

## Setup
```bash
pip install -r requirements.txt
# Windows
./setup/start.ps1
# Linux / macOS
./setup/start.sh
```
Store front: http://localhost:8080

## Run
```bash
python main.py --mock     # pipeline on sample data, no Docker needed
python main.py            # full run against Robot Shop
```
Kill switch: create a file named `STOP` in the repo root, or press Ctrl+C. All active faults are reverted.

## Repository layout
| Folder | Purpose |
|---|---|
| `setup/` | start / reset / health scripts for Robot Shop |
| `mapper/` | dependency map from Compose (`data/depmap.json`) |
| `monitor/` | baseline, fault injection, experiment runner |
| `agent/` | LLM experiment planner and findings analyst |
| `safety/` | allow-list, thresholds, kill switch |
| `data/` | mock and generated JSON (see `contracts.md`) |

## Safety controls
Only containers in the `robot-shop` Compose project can be targeted; faults are limited to an allow-list and 60 s; experiments abort automatically on error-rate, latency or collateral-health thresholds; a kill switch reverts everything.

## Team
See `TEAM.md`. Tools, AI models and third-party libraries used: _fill in before submission_.

## Live dashboard
```
python -m dashboard.server
```
Open http://127.0.0.1:8090. Pick a mode and press **Start run**:
- **Autonomous: test every weakness** runs `heal.run --auto --all --live --pause-first`. The agent reads the code, ranks the weaknesses and tests each one.
- **Autonomous: top weakness only** runs `heal.run --auto --live --pause-before-heal`.
- **Your instruction** runs `heal.run "<instruction>" --live --pause-before-heal`.
- **Replay a recorded run** streams a real campaign log (`dashboard/sample_campaign.log`) without Docker, as a demo backup.

The service map shows each container's live state from Docker. **Heal now** ends the pause on the broken store, and **Kill switch** creates the `STOP` file, which aborts the run and reverts the active fault. The dashboard only reads `heal.run`'s output and never modifies `heal/`. It listens on 127.0.0.1 only.

The older Streamlit view is still available with `streamlit run ui/app.py`.
