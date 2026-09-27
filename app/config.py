"""Central configuration — providers, models, budgets, keys, endpoints.

Everything is a settings value (Agent.MD §10.7). The LLM is a provider *chain*: a primary plus
ordered fallbacks (cloud only, no local). Loaded from environment / `.env`.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM provider chain (cloud only; primary first, then ordered fallbacks) ---
    # providers: gemini | ollama_cloud | openrouter | openai_compatible
    primary_provider: str = "gemini"
    fallback_providers: str = "openrouter,ollama_cloud"   # comma-separated, in order
    # NB: OpenRouter (structured-output-capable free model) is the FIRST fallback; Ollama Cloud's
    # OpenAI-compat endpoint is unreliable for structured output, so it sits last as a last resort.
    llm_temperature: float = 0.0

    # per-provider model (all agent roles share it; override in .env as needed)
    gemini_model: str = "gemini-3.8-flash"
    ollama_model: str = "gpt-oss:120b"
    openrouter_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    openai_model: str = "gpt-4o-mini"

    # --- Credentials / endpoints (secrets come from .env, never from code) ---
    google_api_key: str | None = None

    ollama_api_key: str | None = None
    ollama_base_url: str = "https://ollama.com/v1"        # Ollama Cloud, OpenAI-compatible endpoint

    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    openai_api_key: str | None = None                     # generic OpenAI-compatible
    openai_base_url: str | None = None

    # --- Search (Tavily) ---
    tavily_api_key: str | None = None
    max_results_per_search: int = 5
    per_domain_cap: int = 2                    # one site cannot dominate a report (§5)
    max_page_chars: int = 4000                 # extracted-text cap → protects the token budget

    # --- Secure web fetch / SSRF defense (all deterministic, code-enforced) ---
    # Hostnames resolving to any non-public address are blocked; every redirect is re-validated.
    allow_private_networks: bool = False       # DEV ONLY: set True to permit private/loopback fetch
    fetch_user_agent: str = "RAA-Researcher/1.0 (+https://example.invalid/bot)"
    fetch_connect_timeout: float = 5.0
    fetch_read_timeout: float = 10.0
    fetch_write_timeout: float = 5.0
    fetch_pool_timeout: float = 5.0
    fetch_total_timeout: float = 25.0          # overall wall budget for one URL incl. redirects
    max_redirects: int = 3
    max_response_bytes: int = 3_000_000        # hard cap on bytes read (post-decompression) per page
    max_url_chars: int = 2048
    max_concurrent_fetches: int = 5            # bounded parallelism per search (speed, same sources)
    # comma-separated content-type prefixes we will parse as text
    allowed_content_types: str = "text/html,text/plain,application/xhtml+xml"

    # --- Structured-validation hard limits (§3) ---
    max_question_chars: int = 2000
    max_sub_question_chars: int = 500
    max_query_chars: int = 300
    max_title_chars: int = 300
    max_claim_chars: int = 2000
    max_snippet_chars: int = 1000
    max_findings_per_sub_question: int = 20
    max_findings_total: int = 60
    max_citations: int = 60

    # --- Budgets: HARD caps enforced in the graph, not prompts (§5) ---
    max_sub_questions: int = 5
    max_searches_per_sub_question: int = 3
    max_total_tokens: int = 100_000
    wall_clock_seconds: int = 300             # 5-minute DoD ceiling
    reviewer_send_backs: int = 1              # reviewer may return work exactly once
    max_total_searches: int = 15             # global search calls per run (DoS/cost)
    max_total_fetches: int = 40              # global outbound page fetches per run (DoS/cost)
    max_graph_transitions: int = 60          # hard ceiling on node steps (loop guard)

    # --- Redis (durable run state) ---
    redis_url: str = "redis://localhost:6379/0"
    redis_state_ttl_seconds: int = 86_400     # state/trace expire after 24h by default
    redis_lock_ttl_seconds: int = 600         # per-run worker lock (auto-expires if a worker dies)

    # --- API ---
    api_host: str = "127.0.0.1"               # bind localhost by default; set explicitly to expose
    api_port: int = 8000

    # --- API security (§9) ---
    environment: str = "development"          # "production" hardens defaults (docs off, etc.)
    api_max_body_bytes: int = 16_384          # request-body hard cap (16 KB)
    cors_allow_origins: str = ""              # comma-separated allowlist; empty = no cross-origin
    trusted_hosts: str = "*"                  # comma-separated Host allowlist; set real hosts in prod
    enable_docs: bool = True                  # /docs,/redoc,/openapi.json (auto-off in production)

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")

    def docs_enabled(self) -> bool:
        """Interactive docs/OpenAPI are served only outside production."""
        return self.enable_docs and not self.is_production

    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    def validate_startup(self) -> list[str]:
        """Return production-safety problems with the current config (empty = OK).
        Fatal in production; advisory otherwise."""
        problems: list[str] = []
        if self.is_production:
            if "*" in self.trusted_host_list():
                problems.append("TRUSTED_HOSTS must be an explicit allowlist in production (not '*')")
            if self.allow_private_networks:
                problems.append("ALLOW_PRIVATE_NETWORKS must be false in production")
            if self.enable_docs:
                problems.append("ENABLE_DOCS should be false in production")
            if not (self.google_api_key or self.openrouter_api_key or self.ollama_api_key):
                problems.append("no LLM provider API key configured")
            if not self.tavily_api_key:
                problems.append("TAVILY_API_KEY not configured (search disabled)")
        return problems

    def trusted_host_list(self) -> list[str]:
        return [h.strip() for h in self.trusted_hosts.split(",") if h.strip()] or ["*"]

    def allowed_content_type_prefixes(self) -> tuple[str, ...]:
        """Parsed, lower-cased content-type prefixes the fetch layer will accept as text."""
        return tuple(
            p.strip().lower() for p in self.allowed_content_types.split(",") if p.strip()
        )

    def provider_chain(self) -> list[str]:
        """Ordered, de-duplicated provider list: primary followed by fallbacks."""
        raw = [self.primary_provider, *self.fallback_providers.split(",")]
        chain: list[str] = []
        for name in (p.strip() for p in raw):
            if name and name not in chain:
                chain.append(name)
        return chain


settings = Settings()
