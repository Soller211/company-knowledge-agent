"""Document ingestion: read files, split them into chunks, store chunks with embeddings."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import docx
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200
GENERAL = "general"


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
