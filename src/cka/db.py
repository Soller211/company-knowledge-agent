"""Postgres + pgvector connection and schema."""

import psycopg
from pgvector.psycopg import register_vector

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username      text PRIMARY KEY,
  password_hash text NOT NULL,
  collections   text[] NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS api_keys (
  id          bigserial PRIMARY KEY,
  name        text NOT NULL UNIQUE,
  key_hash    text NOT NULL UNIQUE,
  collections text[] NOT NULL DEFAULT '{}',
  revoked_at  timestamptz
);
"""

# "{dim}" is replaced with str.replace, not str.format: the SQL above contains literal braces.
INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
  path        text PRIMARY KEY,
  collection  text NOT NULL,
  sha256      text NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chunks (
  id          bigserial PRIMARY KEY,
  path        text NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  collection  text NOT NULL,
  page        int,
  content     text NOT NULL,
  embedding   vector({dim}) NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_collection_idx ON chunks (collection);
"""


class EmbeddingModelMismatch(RuntimeError):
    def __init__(self, stored: str, configured: str):
        super().__init__(
            f"El índice se creó con el modelo de embeddings {stored} pero la configuración usa "
            f"{configured}. Ejecuta `cka ingest --reindex` para reconstruirlo."
        )


def init_auth_schema(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(AUTH_SCHEMA)


def init_schema(url: str, dim: int, model: str) -> None:
    init_auth_schema(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(INDEX_SCHEMA.replace("{dim}", str(int(dim))))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('embedding_model', %s), ('embedding_dim', %s) "
            "ON CONFLICT (key) DO NOTHING",
            (model, str(dim)),
        )
        stored = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    if stored["embedding_model"] != model or int(stored["embedding_dim"]) != dim:
        raise EmbeddingModelMismatch(
            f"{stored['embedding_model']} ({stored['embedding_dim']})", f"{model} ({dim})"
        )


def reset_index(url: str) -> None:
    """Drop the document index. Users and API keys are kept."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS chunks, documents, meta")


def connect(url: str) -> psycopg.Connection:
    """Autocommit connection. The schema must already exist."""
    conn = psycopg.connect(url, autocommit=True)
    register_vector(conn)
    # pgvector >= 0.8: keep scanning the HNSW index until LIMIT rows pass the collection
    # filter; otherwise a small collection next to big ones can return too few chunks.
    conn.execute("SET hnsw.iterative_scan = relaxed_order")
    return conn
