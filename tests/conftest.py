import os
import re
import zlib
from dataclasses import replace

import numpy as np
import psycopg
import pytest

from cka import db
from cka.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://cka:cka@localhost:5432/cka_test"
)


class FakeEmbedder:
    """Deterministic bag-of-words embeddings: texts sharing words are similar."""

    model_name = "fake-embedder"
    dim = 256

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"\w+", text.lower()):
                out[i, zlib.crc32(word.encode()) % self.dim] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


def make_settings(**overrides) -> Settings:
    base = Settings(
        database_url=TEST_DATABASE_URL,
        docs_dir="unused",
        secret_key="test-secret",
        min_score=0.0,
    )
    return replace(base, **overrides)


@pytest.fixture
def db_url():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS chunks, documents, users, api_keys, meta CASCADE")
    return TEST_DATABASE_URL


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def conn(db_url, embedder):
    db.init_schema(db_url, embedder.dim, embedder.model_name)
    connection = db.connect(db_url)
    yield connection
    connection.close()


def make_pdf(path, pages):
    """Write a minimal valid PDF with one line of ASCII text per page ("" = no text)."""
    count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(count))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET"
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)
