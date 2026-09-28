"""Document ingestion: read files, split them into chunks, store chunks with embeddings."""

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import docx
import psycopg
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from cka.embeddings import Embedder

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200
GENERAL = "general"

log = logging.getLogger("cka.ingest")


@dataclass(frozen=True)
class Page:
    number: int | None
    text: str


@dataclass(frozen=True)
class Chunk:
    page: int | None
    content: str


def should_index(rel: PurePosixPath) -> bool:
    # "~$" = Office lock files; dot-prefixed = hidden files/folders created by sync clients.
    hidden = any(part.startswith((".", "~$")) for part in rel.parts)
    return rel.suffix.lower() in SUPPORTED_SUFFIXES and not hidden


def collection_for(rel: PurePosixPath) -> str:
    return rel.parts[0].strip().lower() if len(rel.parts) > 1 else GENERAL


def read_file(path: Path) -> list[Page]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages = [
            Page(number, page.extract_text() or "")
            for number, page in enumerate(PdfReader(path).pages, 1)
        ]
    elif suffix == ".docx":
        document = docx.Document(str(path))
        blocks = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            blocks += [" | ".join(cell.text for cell in row.cells) for row in table.rows]
        pages = [Page(None, "\n\n".join(blocks))]
    else:
        pages = [Page(None, path.read_text(encoding="utf-8", errors="replace"))]
    # Postgres text columns reject NUL bytes, which some PDFs produce.
    cleaned = [Page(p.number, p.text.replace("\x00", "")) for p in pages]
    return [p for p in cleaned if p.text.strip()]


def chunk_pages(pages: list[Page]) -> list[Chunk]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    return [
        Chunk(page.number, text)
        for page in pages
        for text in splitter.split_text(page.text)
        if text.strip()
    ]


@dataclass
class IngestReport:
    loaded: list[str] = field(default_factory=list)
    unchanged: int = 0
    removed: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        text = (
            f"{len(self.loaded)} cargados, {self.unchanged} sin cambios, "
            f"{len(self.removed)} eliminados, {len(self.empty)} sin texto, "
            f"{len(self.errors)} con error"
        )
        if self.errors:
            text += ": " + ", ".join(f"{path} ({err})" for path, err in self.errors.items())
        return text


def ingest(conn: psycopg.Connection, docs_dir: Path, embedder: Embedder) -> IngestReport:
    docs_dir = Path(docs_dir)
    # Guard: an unmounted or mistyped folder must not look like "every file was deleted".
    if not docs_dir.is_dir():
        raise FileNotFoundError(f"DOCS_DIR no existe o no es una carpeta: {docs_dir}")
    report = IngestReport()
    known = dict(conn.execute("SELECT path, sha256 FROM documents").fetchall())
    seen: set[str] = set()
    for path in sorted(docs_dir.rglob("*")):
        rel = PurePosixPath(path.relative_to(docs_dir).as_posix())
        if not path.is_file() or not should_index(rel):
            continue
        key = str(rel)
        seen.add(key)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if known.get(key) == digest:
                report.unchanged += 1
                continue
            chunks = chunk_pages(read_file(path))
            vectors = embedder.embed([c.content for c in chunks]) if chunks else []
            _store(conn, key, collection_for(rel), digest, chunks, vectors)
        except Exception as exc:  # noqa: BLE001 - one broken file must not stop the run
            log.warning("No se pudo cargar %s: %s", key, exc)
            report.errors[key] = str(exc)
            continue
        if chunks:
            report.loaded.append(key)
        else:
            log.warning("%s no tiene texto extraíble (¿PDF escaneado?)", key)
            report.empty.append(key)
    for key in sorted(known.keys() - seen):
        conn.execute("DELETE FROM documents WHERE path = %s", (key,))
        report.removed.append(key)
    return report


def _store(conn, path, collection, digest, chunks, vectors) -> None:
    # One transaction per file: a crash never leaves a half-indexed document.
    with conn.transaction():
        conn.execute(
            "INSERT INTO documents (path, collection, sha256) VALUES (%s, %s, %s) "
            "ON CONFLICT (path) DO UPDATE SET collection = EXCLUDED.collection, "
            "sha256 = EXCLUDED.sha256, ingested_at = now()",
            (path, collection, digest),
        )
        conn.execute("DELETE FROM chunks WHERE path = %s", (path,))
        if chunks:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks (path, collection, page, content, embedding) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [
                        (path, collection, chunk.page, chunk.content, vector)
                        for chunk, vector in zip(chunks, vectors, strict=True)
                    ],
                )
