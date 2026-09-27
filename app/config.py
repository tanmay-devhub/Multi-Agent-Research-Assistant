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
    max_page_chars: int = 4000                 # per-page content cap → protects the token budget

    # --- Budgets: HARD caps enforced in the graph, not prompts (§5) ---
    max_sub_questions: int = 5
    max_searches_per_sub_question: int = 3
    max_total_tokens: int = 100_000
    wall_clock_seconds: int = 300             # 5-minute DoD ceiling
    reviewer_send_backs: int = 1              # reviewer may return work exactly once

    # --- Redis (durable run state) ---
    redis_url: str = "redis://localhost:6379/0"

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    def provider_chain(self) -> list[str]:
        """Ordered, de-duplicated provider list: primary followed by fallbacks."""
        raw = [self.primary_provider, *self.fallback_providers.split(",")]
        chain: list[str] = []
        for name in (p.strip() for p in raw):
            if name and name not in chain:
                chain.append(name)
        return chain


settings = Settings()
