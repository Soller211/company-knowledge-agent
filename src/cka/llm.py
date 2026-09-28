"""Chat model factory: any OpenAI-compatible server (NIM, vLLM, Ollama...) or Claude."""

from langchain_core.language_models import BaseChatModel

from cka.config import Settings


def make_llm(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
            max_retries=1,
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "not-needed",
        timeout=settings.llm_timeout,
        max_retries=1,
        temperature=0,
    )
