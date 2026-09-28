import psycopg
import pytest

from cka import db


def public_tables(url):
    with db.connect(url) as conn:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {r[0] for r in rows}


def test_init_schema_is_idempotent(db_url):
    db.init_schema(db_url, 64, "model-a")
    db.init_schema(db_url, 64, "model-a")
    assert {"meta", "documents", "chunks", "users", "api_keys"} <= public_tables(db_url)


def test_init_schema_rejects_different_embedding_model(db_url):
    db.init_schema(db_url, 64, "model-a")
    with pytest.raises(db.EmbeddingModelMismatch, match="reindex"):
        db.init_schema(db_url, 64, "model-b")


def test_init_schema_rejects_different_dimension(db_url):
    db.init_schema(db_url, 64, "model-a")
    with pytest.raises(db.EmbeddingModelMismatch):
        db.init_schema(db_url, 128, "model-a")


def test_reset_index_keeps_users_and_allows_new_model(db_url):
    db.init_schema(db_url, 64, "model-a")
    with db.connect(db_url) as conn:
        conn.execute("INSERT INTO users (username, password_hash) VALUES ('ana', 'x')")
    db.reset_index(db_url)
    db.init_schema(db_url, 32, "model-b")
    with db.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        stored = conn.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
        assert stored[0] == "model-b"


def test_init_auth_schema_does_not_need_embeddings(db_url):
    db.init_auth_schema(db_url)
    tables = public_tables(db_url)
    assert {"users", "api_keys"} <= tables
    assert "chunks" not in tables


def test_connect_enables_iterative_hnsw_scan(conn):
    assert conn.execute("SHOW hnsw.iterative_scan").fetchone()[0] == "relaxed_order"


def test_vector_roundtrip(conn):
    conn.execute(
        "INSERT INTO documents (path, collection, sha256) VALUES ('a.md', 'general', 'h')"
    )
    vector = [1.0] + [0.0] * 255
    conn.execute(
        "INSERT INTO chunks (path, collection, page, content, embedding) "
        "VALUES ('a.md', 'general', NULL, 'hola', %s::vector)",
        (str(vector),),
    )
    score = conn.execute(
        "SELECT 1 - (embedding <=> %s::vector) FROM chunks", (str(vector),)
    ).fetchone()[0]
    assert score == pytest.approx(1.0)


def test_init_schema_upgrades_v01_tables(db_url):
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute(
            "CREATE TABLE users (username text PRIMARY KEY, password_hash text NOT NULL, "
            "collections text[] NOT NULL DEFAULT '{}')"
        )
        c.execute(
            "CREATE TABLE documents (path text PRIMARY KEY, collection text NOT NULL, "
            "sha256 text NOT NULL, ingested_at timestamptz NOT NULL DEFAULT now())"
        )
        c.execute("INSERT INTO users (username, password_hash) VALUES ('ana', 'x')")
        c.execute("INSERT INTO documents (path, collection, sha256) VALUES ('a.md', 'general', 'h')")
    db.init_schema(db_url, 64, "model-a")
    with db.connect(db_url) as conn:
        assert conn.execute("SELECT status, error FROM documents").fetchone() == ("ready", None)
        assert conn.execute("SELECT is_admin FROM users").fetchone() == (False,)
        conn.execute("INSERT INTO documents (path, collection, sha256) VALUES ('b.md', 'general', NULL)")
