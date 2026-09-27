# Multi-Agent Research Assistant (RAA)

Give it a research question, get back a **fully-cited report** — with **durable state** (survives a
crash/restart), **hard spend budgets**, and a **step-level trace** for every run. Supervisor/worker
topology on LangGraph.

Full spec & rules: [`../Agent.MD`](../Agent.MD). Task backlog: [`../Task.MD`](../Task.MD).

## Status
**All phases (0–5) complete and live-verified, plus production security hardening and three
extensions** (2026-09-27). The pipeline runs end-to-end (cited report with intact provenance),
Redis persistence + resume-without-replay + provider failover confirmed, the FastAPI API + SSE +
browser UI are live, and the security suite is green (`pytest tests/security/ -v` → 64 passed).
Extensions: cross-source **contradiction detection**, source **credibility scoring**, and a
**red-team test suite**. See [`SECURITY.md`](../SECURITY.md).

> Note: on the free LLM tier a full run is latency-bound (~5–10 min) and may end in a graceful
> `*_exhausted`/`failed` terminal state under quota load (findings preserved, never a crash).

## Stack
Python 3.11–3.14 · LangGraph · Tavily · Redis · FastAPI · Pydantic v2 · LLM: **cloud provider chain**
(primary + automatic fallbacks).

## LLM provider chain (cloud only, no local)
The system tries the primary provider first and **falls back automatically** on any error (with
`max_retries=0`, so failover is immediate). Set the order with `PRIMARY_PROVIDER` and
`FALLBACK_PROVIDERS` in `.env` — no code change.

| order | provider            | model (default)                             | notes                                   |
|-------|---------------------|---------------------------------------------|-----------------------------------------|
| 1     | `gemini`            | `gemini-3.8-flash`                          | primary; free tier ≈ **20 req/day**     |
| 2     | `openrouter`        | `nvidia/nemotron-3-super-120b-a12b:free`    | first fallback; free, structured-output |
| 3     | `ollama_cloud`      | `gpt-oss:120b` (OpenAI-compat `/v1`)        | last resort (structured output flaky)   |
| —     | `openai_compatible` | any (`OPENAI_BASE_URL`)                     | optional                                |

Why this order: the pipeline is dominated by *structured-output* calls. Gemini is highest-quality
but its free tier is tiny, so **OpenRouter is the first fallback** (a currently-free model that
reliably emits structured output). Ollama Cloud is last because its OpenAI-compatible endpoint does
not do structured output reliably.

> ⚠️ **Free-tier limits are real.** Gemini free ≈ 20 requests/day; OpenRouter free models throttle
> ~per-minute; free model IDs rotate (both `gemini-2.5-flash` and `deepseek…:free` went dead during
> the build). A run makes ~10 LLM calls, so for real throughput use a paid Gemini tier or OpenRouter
> credits. Failover keeps runs graceful (partial report) rather than crashing when a tier is spent.

## Setup
```bash
cd RAA
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example .env            # then fill in keys
```
- Keys: Gemini https://aistudio.google.com/apikey · OpenRouter https://openrouter.ai/keys ·
  Ollama Cloud https://ollama.com/settings/keys · Tavily https://app.tavily.com (search).
- Redis (required for persistence/resume): `docker run -d --name raa-redis -p 6379:6379 redis:7-alpine`
  (`docker start raa-redis` to bring it back). If Redis is down, runs still work but without
  persistence or crash-resume.

## Run
```bash
uvicorn app.main:app          # do NOT use --reload in production
# UI:    http://localhost:8000/        (ask a question, watch live progress, read the cited report)
# health: /health  ·  readiness: /ready  ·  docs (dev only): /docs
```
Or drive the graph directly, no server:
```bash
.venv\Scripts\python -c "from app.graph.builder import run_research; s=run_research('Your question?'); print(s.status); print(s.report.body)"
# resume a run (continues from the last node, no replay):
.venv\Scripts\python -c "from app.graph.builder import resume_research; print(resume_research('<run_id>').status)"
```
Production: use the hardened `docker-compose.yml` (`ENVIRONMENT=production` disables docs, enforces
config validation; Redis stays internal-only; API binds host loopback).

## API
- `POST /research` → `202` + `run_id` (async; runs on a background worker)
- `GET /research/{id}` → status + cited report (`citations_valid`, source/finding/token counts)
- `GET /research/{id}/trace` → full step log (agent, output, tokens, latency)
- `GET /research/{id}/stream` → **SSE** live progress
- `POST /research/{id}/cancel` → durable cancellation (`cancelled` state, findings kept)
- `GET /` → browser UI · `GET /health` (liveness) · `GET /ready` (readiness)

Run-scoped endpoints pass through an authorization hook — **knowing a `run_id` is not
authorization** (single-user permissive by default; wire real auth in `app/api/security.py`).

## How it works
1. **Planner** (LLM) → splits the question into focused sub-questions (`app/agents/planner.py`).
2. **Researcher** (LLM + web) → per sub-question: plans queries, Tavily searches, **fetches &
   extracts real pages** (snippets not trusted), synthesizes `Finding`s with provenance
   (`app/agents/researcher.py`, `app/tools/search.py`).
3. **Supervisor** (pure code, no LLM) → reviews, routes, retries once, enforces budgets
   (`app/graph/supervisor.py`).
4. **Analyze** (LLM) → flags cross-source **contradictions** for the same sub-question
   (`app/agents/contradiction.py`); the writer surfaces them with `[CONFLICTING]` markers.
5. **Writer** (LLM) → cited report; citations validated against real findings; prefers
   higher-**credibility** sources (`app/credibility.py`) and flags low-credibility ones.

Graph: `planner → researcher → review → analyze → writer`, wired in `app/graph/builder.py`.
`RunState` (`app/schemas/models.py`) is both the graph state and the persisted object.

## Security
Production-hardened: SSRF-safe web fetch (single egress, private/metadata-IP blocking, redirect
re-validation), deterministic prompt-injection isolation, a full provenance chain (every citation →
finding → fetched source), hard budgets with explicit terminal states, safe Redis state (versioned,
validated, locked), and a hardened API (strict schemas, CSP/security headers, sanitized errors,
authz/quota hooks). Full model: [`SECURITY.md`](../SECURITY.md). Deploy with the hardened
`docker-compose.yml` (non-root, read-only FS, internal-only Redis). Scan with `pip-audit` / `bandit`
(`requirements-dev.txt`).

## What makes this real (not a demo)
- Budgets enforced in code (`app/config.py`), never in prompts: max sub-questions, searches/sub-q,
  total tokens, 5-min wall-clock, one reviewer send-back, per-domain cap, URL dedup.
- External failures are **states** (`no_results`/`rate_limited`/`paywalled`/`timed_out`) the
  supervisor routes around; planner/researcher/writer errors degrade to a partial-but-valid result.
- Every finding carries provenance; every claim is a citation validated against a real finding.
- Run state persisted to Redis after every node → a killed process **resumes, it does not replay**
  (verified live via `resume_research`).
- Cross-source **contradictions** are detected and surfaced; sources carry a **credibility score**
  the writer uses to prefer authoritative evidence and flag weak sources.
- Security is deterministic and code-enforced (SSRF, provenance, budgets, redaction) — not
  prompt-based — and covered by an offline red-team test suite.

## Tests
Tests + dev tooling live in the **parent folder** (`../`) so this folder stays deployable. Run from
the parent (`D:\RA`) using this project's venv:
```bash
RAA\.venv\Scripts\python -m pip install -r requirements-dev.txt
RAA\.venv\Scripts\python -m pytest tests/security/ -v   # 64 offline red-team tests (all mocked)
pip-audit -r RAA/requirements.txt   # dependency CVE scan
bandit -r RAA/app                   # static security lint
```

## Layout
`app/` — `agents/` (planner, researcher, writer, contradiction), `graph/` (builder + supervisor),
`schemas/` (Pydantic contracts), `tools/` (Tavily search + fetch/extract), `security/` (SSRF-safe
fetch, URL normalization, secret redaction), `store/` (Redis persistence), `llm/` (provider chain +
token meter), `api/` (routes, security middleware/hooks, `index.html` UI), `credibility.py`,
`config.py`, `main.py`. Docker: `Dockerfile`, `docker-compose.yml`. The red-team suite
(`tests/security/`), `pytest.ini`, and `requirements-dev.txt` live in the **parent** folder (`../`),
outside the deployable project. See `../Agent.MD` §13 and [`SECURITY.md`](../SECURITY.md).
