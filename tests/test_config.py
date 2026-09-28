import pytest

from cka.config import Settings


def test_requires_secret_key():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings.from_env({})


def test_blank_secret_key_is_rejected():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings.from_env({"SECRET_KEY": "   "})


def test_rejects_unknown_llm_provider():
    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        Settings.from_env({"SECRET_KEY": "s", "LLM_PROVIDER": "gemini"})


def test_defaults():
    s = Settings.from_env({"SECRET_KEY": "s"})
    assert s.llm_provider == "openai"
    assert s.llm_base_url is None
    assert s.embedding_model == "BAAI/bge-m3"
    assert s.embedding_base_url is None
    assert s.top_k == 6
    assert s.min_score == 0.35
    assert s.llm_timeout == 60.0
    assert s.not_found_message == "No encontré esa información en los documentos."
    assert s.cookie_secure is False


def test_parses_values():
    s = Settings.from_env(
        {
            "SECRET_KEY": "s",
            "LLM_PROVIDER": "Anthropic",
            "LLM_BASE_URL": "",
            "TOP_K": "3",
            "MIN_SCORE": "0.5",
            "LLM_TIMEOUT": "10",
            "EMBEDDING_BASE_URL": "http://embed:8000/v1",
            "COOKIE_SECURE": "true",
            "DOCS_DIR": "/data/docs",
        }
    )
    assert s.llm_provider == "anthropic"
    assert s.llm_base_url is None
    assert s.top_k == 3
    assert s.min_score == 0.5
    assert s.llm_timeout == 10.0
    assert s.embedding_base_url == "http://embed:8000/v1"
    assert s.cookie_secure is True
    assert s.docs_dir == "/data/docs"
