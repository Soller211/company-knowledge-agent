"""Settings read from environment variables (.env)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

PROVIDERS = ("openai", "anthropic")


@dataclass(frozen=True)
class Settings:
    database_url: str
    docs_dir: str
    secret_key: str
    llm_provider: str = "openai"
    llm_base_url: str | None = None
    llm_model: str = "qwen2.5-72b-instruct"
    llm_api_key: str = ""
    llm_timeout: float = 60.0
    embedding_model: str = "BAAI/bge-m3"
    embedding_base_url: str | None = None
    embedding_api_key: str = ""
    top_k: int = 6
    min_score: float = 0.35
    not_found_message: str = "No encontré esa información en los documentos."
    cookie_secure: bool = False
    max_upload_mb: int = 50

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        secret = env.get("SECRET_KEY", "").strip()
        if not secret:
            raise RuntimeError("SECRET_KEY es obligatoria: defínela en el .env")
        provider = env.get("LLM_PROVIDER", cls.llm_provider).strip().lower()
        if provider not in PROVIDERS:
            raise RuntimeError(f"LLM_PROVIDER debe ser 'openai' o 'anthropic', no {provider!r}")
        return cls(
            database_url=env.get("DATABASE_URL", "postgresql://cka:cka@localhost:5432/cka"),
            docs_dir=env.get("DOCS_DIR", "./examples/docs"),
            secret_key=secret,
            llm_provider=provider,
            llm_base_url=env.get("LLM_BASE_URL") or None,
            llm_model=env.get("LLM_MODEL", cls.llm_model),
            llm_api_key=env.get("LLM_API_KEY", ""),
            llm_timeout=float(env.get("LLM_TIMEOUT", cls.llm_timeout)),
            embedding_model=env.get("EMBEDDING_MODEL", cls.embedding_model),
            embedding_base_url=env.get("EMBEDDING_BASE_URL") or None,
            embedding_api_key=env.get("EMBEDDING_API_KEY", ""),
            top_k=int(env.get("TOP_K", cls.top_k)),
            min_score=float(env.get("MIN_SCORE", cls.min_score)),
            not_found_message=env.get("NOT_FOUND_MESSAGE", cls.not_found_message),
            cookie_secure=env.get("COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes"),
            max_upload_mb=int(env.get("MAX_UPLOAD_MB", cls.max_upload_mb)),
        )
