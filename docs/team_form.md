# Team and technology form

## Team

**Team name:** TEAM 17

| Member | Role |
|---|---|
| Thanay Ballal | AI agents: code review, planner, analyst, patcher |
| Naveena Eliza | Setup, live fixes, UI |
| Akul Sajith | Dependency mapper, docs, presentation |
| Gargi Khandekar | Monitoring, fault injection, cart-survival probe, comparison |
| Rohit Nandagopal | Safety guard, LangGraph heal/harden orchestration |

## Tools

| Tool | Use |
|---|---|
| Claude Code | AI coding assistant |
| OpenAI Codex | AI coding assistant |
| LangGraph | Heal/harden agent graph orchestration |
| Docker Compose | Running Robot Shop locally; fault injection and live fixes on its containers |

## AI models

| Model | Use |
|---|---|
| gpt-oss:120b via Ollama Cloud | Default model for the code review, chaos, diagnosis, remediation, analyst and patcher agents |
| Google Gemini | Also supported (`LLM_PROVIDER=gemini`) |

Every LLM agent has a deterministic fallback, so the pipeline still runs without a key or when the API quota is exhausted.

## Third-party software

### Python libraries (`requirements.txt`)

| Library | Version constraint | Use |
|---|---:|---|
| Docker SDK for Python (`docker`) | `>=7.0` | Container discovery, fault injection, live fixes and recovery |
| PyYAML (`pyyaml`) | `>=6.0` | Docker Compose parsing |
| Requests (`requests`) | `>=2.31` | Load traffic, probes and monitoring |
| pytest | `>=8.0` | Automated tests |
| OpenAI Python SDK (`openai`) | `>=1.0` | OpenAI-compatible client for Ollama Cloud (and OpenAI) |
| Google Gen AI SDK (`google-genai`) | `>=1.0` | Gemini support |
| LangGraph (`langgraph`) | `>=1.0` | Heal/harden state graph |

### System under test

[Stan's Robot Shop](https://github.com/instana/robot-shop) (github.com/instana/robot-shop) is licensed under the **Apache License 2.0**. It is cloned and run separately and is never modified. Its Apache-2.0 license does not replace the license terms of its own third-party dependencies.
