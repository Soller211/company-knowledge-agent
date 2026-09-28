import sys

import numpy as np
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from cka.embeddings import RemoteEmbedder, make_embedder
from cka.llm import make_llm
from conftest import make_settings


def test_remote_embedder_normalizes_and_detects_dimension(monkeypatch):
    monkeypatch.setattr(
        "langchain_openai.OpenAIEmbeddings.embed_documents",
        lambda self, texts: [[3.0, 4.0]] * len(texts),
    )
    embedder = make_embedder(
        make_settings(embedding_base_url="http://embed:8000/v1", embedding_model="nv-embed")
    )
    assert isinstance(embedder, RemoteEmbedder)
    assert embedder.model_name == "nv-embed"
    assert embedder.dim == 2
    np.testing.assert_allclose(embedder.embed(["a", "b"]), [[0.6, 0.8], [0.6, 0.8]], rtol=1e-6)


def test_local_embedder_without_extra_gives_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(RuntimeError, match="--extra local"):
        make_embedder(make_settings())


def test_openai_compatible_llm_uses_base_url():
    llm = make_llm(
        make_settings(llm_provider="openai", llm_base_url="http://nim:8000/v1", llm_model="qwen")
    )
    assert isinstance(llm, ChatOpenAI)
    assert llm.openai_api_base == "http://nim:8000/v1"
    assert llm.model_name == "qwen"


def test_anthropic_llm():
    llm = make_llm(
        make_settings(llm_provider="anthropic", llm_model="claude-sonnet-5", llm_api_key="k")
    )
    assert isinstance(llm, ChatAnthropic)
    assert llm.model == "claude-sonnet-5"
