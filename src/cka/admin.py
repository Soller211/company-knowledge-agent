"""Document management for the admin panel: validate, store, list and delete files."""

import os
import re
from pathlib import Path, PurePosixPath

import psycopg

from cka.ingest import GENERAL, SUPPORTED_SUFFIXES

COLLECTION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CHUNK_BYTES = 1024 * 1024


class InvalidUpload(ValueError):
    """Rejected upload or path; the message is shown to the admin."""


class UploadTooLarge(InvalidUpload):
    pass


def validate_collection(name: str) -> str:
    name = name.strip().lower()
    if not COLLECTION_RE.fullmatch(name):
        raise InvalidUpload(
            "Nombre de colección no válido: usa minúsculas sin acentos, números, - o _"
        )
    return name


def validate_filename(name: str) -> str:
    dangerous = (
        name in ("", ".", "..")
        or name.startswith((".", "~$"))
        or "/" in name
        or "\\" in name
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    )
    if dangerous:
        raise InvalidUpload("Nombre de archivo no válido")
    if PurePosixPath(name).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise InvalidUpload("Formato no soportado: sube PDF, Word (.docx), Markdown o TXT")
    return name


def collection_dir(docs_dir: Path, collection: str) -> Path:
    """The existing folder for a collection (case-insensitive, e.g. RRHH/), or a new one."""
    for child in docs_dir.iterdir():
        if child.is_dir() and child.name.strip().lower() == collection:
            return child
    return docs_dir / collection


def _inside(docs_dir: Path, target: Path) -> bool:
    return target.resolve().is_relative_to(docs_dir.resolve())


def save_upload(
    docs_dir: Path, collection: str, filename: str, source, max_bytes: int
) -> PurePosixPath:
    """Write an uploaded file into its collection folder atomically.

    Returns its path relative to docs_dir. A file with the same name is replaced.
    """
    collection = validate_collection(collection)
    filename = validate_filename(filename)
    folder = collection_dir(docs_dir, collection)
    target = folder / filename
    if not _inside(docs_dir, target):
        raise InvalidUpload("Nombre de archivo no válido")
    folder.mkdir(exist_ok=True)
    # Dot-prefixed temp name: ingestion ignores it while it is being written.
    temp = folder / f".{filename}.upload"
    written = 0
    try:
        with temp.open("wb") as out:
            while chunk := source.read(CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    raise UploadTooLarge(f"El archivo supera {max_bytes // (1024 * 1024)} MB")
                out.write(chunk)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return PurePosixPath(target.relative_to(docs_dir).as_posix())


def delete_document(conn: psycopg.Connection, docs_dir: Path, path: str) -> None:
    if conn.execute("SELECT 1 FROM documents WHERE path = %s", (path,)).fetchone() is None:
        raise LookupError(path)
    target = docs_dir / path
    if not _inside(docs_dir, target):
        raise InvalidUpload("Ruta no válida")
    target.unlink(missing_ok=True)
    conn.execute("DELETE FROM documents WHERE path = %s", (path,))


def list_documents(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT path, collection, status, error, ingested_at FROM documents "
        "ORDER BY collection, path"
    ).fetchall()
    return [
        {"path": p, "collection": c, "status": s, "error": e, "updated_at": t.isoformat()}
        for p, c, s, e, t in rows
    ]


def list_collections(conn: psycopg.Connection, docs_dir: Path) -> list[str]:
    """Collections an admin can upload to: existing folders, indexed ones and general.

    Folder names that are not valid collection names (e.g. with spaces) are left out:
    uploading to them would be rejected anyway.
    """
    folders = set()
    if docs_dir.is_dir():
        folders = {
            child.name.strip().lower()
            for child in docs_dir.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        }
    indexed = {row[0] for row in conn.execute("SELECT DISTINCT collection FROM documents")}
    names = folders | indexed | {GENERAL}
    return sorted(name for name in names if COLLECTION_RE.fullmatch(name))
