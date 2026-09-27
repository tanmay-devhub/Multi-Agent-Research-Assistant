# Multi-Agent Research Assistant

Give it a research question, get back a **fully-cited report** — every claim traced to a real,
fetched web source. Built as a supervisor/worker multi-agent system on **LangGraph**, with durable
state, hard spend budgets, a step-level trace, live streaming, and production security hardening.

> **Status:** complete and verified end-to-end. On a free LLM tier a full run is latency-bound
> (~5–10 min) and may end in a graceful `*_exhausted` / `failed` state under quota limits — findings
> are preserved and the process never crashes.

---

## What it does

```
question
   │
   ▼
 planner ──► researcher ──► review ──► analyze ──► writer ──► cited report
 (LLM)       (LLM+web)     (code)      (LLM)       (LLM)
```

1. **Planner** splits the question into focused, searchable sub-questions.
2. **Researcher** plans queries, discovers URLs via Tavily, **fetches and extracts the real page**
   (search snippets are never trusted as evidence), and synthesizes `Finding`s with provenance.
3. **Supervisor** (pure code, no LLM) reviews each result, routes, retries once, and enforces budgets.
4. **Analyze** detects **cross-source contradictions** for the same sub-question.
5. **Writer** produces the report with inline citations validated against real findings, preferring
   higher-**credibility** sources and flagging weak ones; contradictions are surfaced with
   `[CONFLICTING]` markers.

State is persisted to Redis after every node, so a killed run **resumes from where it stopped —
without replaying** completed work.

## Stack

Python 3.11+ · LangGraph · Tavily · Redis · FastAPI + SSE · Pydantic v2 · httpx · an LLM **provider
chain** (cloud only, automatic fallback).

## LLM provider chain

The primary provider is tried first and **fails over automatically** on any error (`max_retries=0`,
so failover is immediate). Order and models are set via `PRIMARY_PROVIDER` / `FALLBACK_PROVIDERS` in
`.env` — no code change.

| order | provider            | model (default)                          | notes                                   |
|-------|---------------------|------------------------------------------|-----------------------------------------|
| 1     | `gemini`            | `gemini-3.8-flash`                       | primary; free tier ≈ 20 req/day         |
| 2     | `openrouter`        | `nvidia/nemotron-3-super-120b-a12b:free` | first fallback; free, structured-output |
| 3     | `ollama_cloud`      | `gpt-oss:120b` (OpenAI-compatible)       | last resort                             |
| —     | `openai_compatible` | any (`OPENAI_BASE_URL`)                  | optional                                |

> ⚠️ Free tiers throttle hard and free model IDs rotate — verify the model still exists. For real
> throughput use a paid Gemini tier or OpenRouter credits. Failover keeps a run graceful (partial
> report) rather than crashing when a tier is exhausted.

## Quickstart

```bash
git clone https://github.com/<your-user>/Multi-Agent-Research-Assistant.git
cd Multi-Agent-Research-Assistant

python -m venv .venv
.venv\Scripts\activate            # Windows  ·  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

cp .env.example .env              # then fill in your keys

# Redis (for persistence / resume):
docker run -d --name raa-redis -p 6379:6379 redis:7-alpine

uvicorn app.main:app              # do NOT use --reload in production
# open http://localhost:8000/
```

Keys: [Gemini](https://aistudio.google.com/apikey) ·
[OpenRouter](https://openrouter.ai/keys) ·
[Ollama Cloud](https://ollama.com/settings/keys) ·
[Tavily](https://app.tavily.com) (search). If Redis is down, runs still work but without persistence
or resume.

## Usage

**Browser UI** — `GET /` : ask a question, watch live progress stream in, read the cited report.

**API**

| endpoint | purpose |
|----------|---------|
| `POST /research` | `202` + `run_id` (runs asynchronously on a background worker) |
| `GET /research/{id}` | status + cited report (`citations_valid`, counts, tokens) |
| `GET /research/{id}/trace` | full step log (agent, output, tokens, latency) |
| `GET /research/{id}/stream` | live progress via Server-Sent Events |
| `POST /research/{id}/cancel` | durable cancellation (findings preserved) |
| `GET /health` · `GET /ready` | liveness · readiness |

Run-scoped endpoints pass through an authorization hook — **knowing a `run_id` is not
authorization** (permissive single-user by default; wire real auth in `app/api/security.py`).

**Without the server** — drive the graph directly:

```bash
python -c "from app.graph.builder import run_research; s = run_research('Your question?'); print(s.status); print(s.report.body)"
python -c "from app.graph.builder import resume_research; print(resume_research('<run_id>').status)"  # resume, no replay
```

## What makes it production-grade

- **Budgets enforced in code, never in prompts** (`app/config.py`): max sub-questions,
  searches/sub-question, total searches/fetches, findings, citations, tokens, wall-clock, graph
  steps, per-domain cap, URL dedup. Exhaustion ends in an explicit terminal state.
- **Failure-as-state**: search/fetch/LLM failures become states the supervisor routes around; the
  run always produces a partial-but-valid result, never a crash.
- **Provenance**: every finding is bound to a real fetched source (`source_id` + SHA-256); every
  citation is validated against a real finding before the report ships — invented/orphan citations
  are dropped.
- **Durable state & resume**: state written to Redis after every node; a killed process resumes
  without replaying completed work; per-run locking prevents duplicate execution.
- **Contradiction detection** and **credibility scoring** across sources.

## Security

Security is **deterministic and code-enforced**, not prompt-based — an LLM can be told anything, but
these controls sit outside its reach:

- **SSRF-safe fetch** (`app/security/`): a single outbound egress that resolves hosts and blocks
  loopback / private / link-local / cloud-metadata addresses (IPv4 + IPv6), re-validates every
  redirect, enforces timeouts + response-size caps + a content-type allowlist, and sends no
  cookies/auth/proxy. Snippets and search results are treated as data, never instructions.
- **Strict Pydantic validation** at every boundary with hard size/count limits; no
  `eval`/`exec`/`pickle`/`shell=True`.
- **Secret redaction** across traces, logs, errors, and API responses.
- **Hardened API**: strict request/response schemas, body-size limit, sanitized error handlers with
  request IDs, security headers + CSP, trusted-host and CORS controls, docs disabled in production.
- **Hardened deployment**: `docker-compose.yml` runs non-root, read-only filesystem, dropped
  capabilities, internal-only Redis, and no secrets baked into the image.

## Configuration

All settings come from environment / `.env` (see `.env.example`). Highlights: provider chain +
model IDs, budgets, secure-fetch limits, Redis URL + TTLs, and `ENVIRONMENT=production` (disables
docs and enforces startup config validation).

## Deployment

```bash
# set REDIS_PASSWORD and provider keys in your environment or .env, then:
docker compose up --build
```

The build context ships only `app/` + pinned runtime dependencies — no dev tooling, no secrets.

## Project layout

```
app/
  agents/     planner · researcher · writer · contradiction
  graph/      builder (state graph) · supervisor (routing/review/budgets)
  schemas/    Pydantic data contracts + RunState
  tools/      Tavily search + page fetch/extract
  security/   SSRF-safe fetch · URL normalization · secret redaction
  store/      Redis persistence, locking, cancellation
  llm/        provider chain + token metering
  api/        routes · security middleware/hooks · browser UI
  credibility.py · config.py · main.py
Dockerfile · docker-compose.yml · requirements.txt · .env.example
```
