# Multi-Agent Research Assistant (RAA)

Give it a research question, get back a **fully-cited report** — with **durable state** (survives a
crash/restart), **hard spend budgets**, and a **step-level trace** for every run. Supervisor/worker
topology on LangGraph.

Full spec & rules: [`../Agent.MD`](../Agent.MD). Task backlog: [`../Task.MD`](../Task.MD).

## Status
**Phases 0–4 complete and verified live** (2026-09-27): the full pipeline ran end-to-end (a sample
question produced 4 sub-questions, 22 findings, 22/22 cited, ~4 min), with Redis persistence,
resume-without-replay, and provider failover all confirmed. **Phase 5** (the research API +
streaming + browser UI) is not built yet — only `/health` is live so far.

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
The research **API** is Phase 5. Until then, drive the graph directly:
```bash
# one full run (plan → research → write), state mirrored to Redis:
.venv\Scripts\python -c "from app.graph.builder import run_research; s=run_research('Your question?'); print(s.status); print(s.report.body)"

# resume a run by id (continues from the last node, no replay):
.venv\Scripts\python -c "from app.graph.builder import resume_research; print(resume_research('<run_id>').status)"
```
The FastAPI server currently exposes only the health check:
```bash
uvicorn app.main:app --reload
# http://localhost:8000/health  -> {"status":"ok","provider_chain":[...]}
# http://localhost:8000/docs    -> Swagger UI
```

## API (arrives in Phase 5 — not built yet)
- `POST /research` → returns `run_id` immediately (async)
- `GET /research/{id}` → status, then the finished cited report
- `GET /research/{id}/trace` → full step log (agent, input, output, tools, tokens, latency)

## How it works
1. **Planner** (LLM) → splits the question into focused sub-questions (`app/agents/planner.py`).
2. **Researcher** (LLM + web) → per sub-question: plans queries, Tavily searches, **fetches &
   extracts real pages** (snippets not trusted), synthesizes `Finding`s with provenance
   (`app/agents/researcher.py`, `app/tools/search.py`).
3. **Supervisor** (pure code, no LLM) → reviews, routes, retries once, enforces budgets
   (`app/graph/supervisor.py`).
4. **Writer** (LLM) → cited report; citations validated against real findings (`app/agents/writer.py`).

The graph is wired in `app/graph/builder.py`; `RunState` (`app/schemas/models.py`) is both the
graph state and the persisted object.

## What makes this real (not a demo)
- Budgets enforced in code (`app/config.py`), never in prompts: max sub-questions, searches/sub-q,
  total tokens, 5-min wall-clock, one reviewer send-back, per-domain cap, URL dedup.
- External failures are **states** (`no_results`/`rate_limited`/`paywalled`/`timed_out`) the
  supervisor routes around; planner/researcher/writer errors degrade to a partial-but-valid result.
- Every finding carries provenance; every claim is a citation validated against a real finding.
- Run state persisted to Redis after every node → a killed process **resumes, it does not replay**
  (verified live via `resume_research`).

## Layout
`app/` — `agents/` (planner, researcher, writer), `graph/` (builder + supervisor), `schemas/`
(Pydantic contracts), `tools/` (Tavily search + fetch/extract), `store/` (Redis persistence),
`llm/` (provider chain + token meter), `config.py`, `main.py`. `api/` is empty until Phase 5.
See `../Agent.MD` §13.
