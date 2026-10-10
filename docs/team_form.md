# Team and technology form

## Team

| Member | Workstream |
|---|---|
| Naveena Eliza | P1 — Robot Shop application and Docker setup |
| Akul Sajith | P2 — dependency mapper, source discovery, reporting and documentation |
| Gargi Khandekar | P3 — monitoring, fault injection runner and comparison |
| Thanay Ballal | P4 — AI code review, experiment planner, analyst and patcher |
| Rohit Nandagopal | P5 — safety controls, recovery and pipeline integration |

## Development and operations tools

| Tool | Use |
|---|---|
| Claude Code | Agent, monitoring and integration implementation support |
| OpenAI Codex | Mapper, reporting, testing and documentation implementation support |
| Git and GitHub | Version control, branches and pull-request integration |
| Docker Desktop and Docker Compose | Local Robot Shop deployment and isolated fault execution |
| Instana | Optional application performance monitoring, traces and infrastructure telemetry |

## AI model

| Model | Use |
|---|---|
| Google Gemini | Structured source-code review, experiment planning, findings analysis and remediation proposals |

The pipeline retains deterministic fallbacks so that planning, analysis and reporting can still run when the model or API quota is unavailable.

## Direct Python dependencies

These entries are taken from the repository's `requirements.txt`.

| Dependency | Version constraint | Use |
|---|---:|---|
| Docker SDK for Python (`docker`) | `>=7.0` | Container discovery, fault injection and recovery |
| PyYAML (`pyyaml`) | `>=6.0` | Docker Compose parsing and override generation |
| Requests (`requests`) | `>=2.31` | HTTP load, probes and monitoring |
| pytest | `>=8.0` | Automated tests and contract checks |
| Google Gen AI SDK (`google-genai`) | `>=1.0` | Gemini API integration and structured output |

## Robot Shop third-party application

[Robot Shop](https://github.com/instana/robot-shop) is the open-source system under test and is licensed under the **Apache License 2.0**. Its main application libraries include:

| Runtime | Libraries used by Robot Shop |
|---|---|
| Node.js | Express, body-parser, MongoDB and Redis clients, request, Pino, Prometheus client, Instana collector |
| Python | Flask, uWSGI, Requests, Pika, Prometheus client, OpenTracing, Instana, Locust |
| Java | Spring Boot, Spring Data JPA, Spring Retry, Actuator, MySQL connector, Apache HttpClient, Instana Java SDK |

Robot Shop's Apache-2.0 project license does not replace the individual license terms of its third-party dependencies.
