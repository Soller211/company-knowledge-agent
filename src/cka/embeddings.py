"""Embedding models: bge-m3 running locally, or any OpenAI-compatible endpoint."""

from typing import Protocol

import numpy as np

from cka.config import Settings


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


def _normalize(vectors) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return array / norms


class LocalEmbedder:
    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "El modelo de embeddings local necesita el extra 'local' "
                "(uv sync --extra local), o define EMBEDDING_BASE_URL."
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.model_name = model_name
        self.dim = self.embed(["dimension probe"]).shape[1]

    def embed(self, texts: list[str]) -> np.ndarray:
        return _normalize(self._model.encode(texts, batch_size=16))


class RemoteEmbedder:
    def __init__(self, model_name: str, base_url: str, api_key: str):
        from langchain_openai import OpenAIEmbeddings

        self._client = OpenAIEmbeddings(
            model=model_name,
            base_url=base_url,
            api_key=api_key or "not-needed",
            # Non-OpenAI servers: send raw text instead of tiktoken token ids.
            check_embedding_ctx_length=False,
        )
        self.model_name = model_name
        self.dim = self.embed(["dimension probe"]).shape[1]

    def embed(self, texts: list[str]) -> np.ndarray:
        return _normalize(self._client.embed_documents(texts))


def make_embedder(settings: Settings) -> Embedder:
    if settings.embedding_base_url:
        return RemoteEmbedder(
            settings.embedding_model, settings.embedding_base_url, settings.embedding_api_key
        )
    return LocalEmbedder(settings.embedding_model)
