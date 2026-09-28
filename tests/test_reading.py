from pathlib import PurePosixPath

import docx
import pytest

from cka.ingest import (
    CHUNK_SIZE,
    Chunk,
    Page,
    chunk_pages,
    collection_for,
    read_file,
    should_index,
)
from conftest import make_pdf


def test_read_pdf_keeps_page_numbers_and_skips_blank_pages(tmp_path):
    path = tmp_path / "manual.pdf"
    make_pdf(path, ["first page text", "", "third page text"])
    pages = read_file(path)
    assert [(p.number, p.text.strip()) for p in pages] == [
        (1, "first page text"),
        (3, "third page text"),
    ]


def test_read_docx_joins_paragraphs_and_tables(tmp_path):
    path = tmp_path / "policy.docx"
    document = docx.Document()
    document.add_paragraph("Hello")
    document.add_paragraph("")
    document.add_paragraph("World")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Plan"
    table.rows[0].cells[1].text = "22 days"
    document.save(path)
    assert read_file(path) == [Page(None, "Hello\n\nWorld\n\nPlan | 22 days")]


def test_read_text_file_strips_nul_bytes(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("a\x00b", encoding="utf-8")
    assert read_file(path) == [Page(None, "ab")]


def test_chunks_never_cross_pages():
    chunks = chunk_pages([Page(1, "alpha " * 400), Page(2, "beta " * 400)])
    assert len(chunks) > 2
    for chunk in chunks:
        expected = {"alpha"} if chunk.page == 1 else {"beta"}
        assert set(chunk.content.split()) == expected
        assert len(chunk.content) <= CHUNK_SIZE


def test_consecutive_chunks_overlap():
    text = " ".join(f"w{i}" for i in range(1000))
    chunks = chunk_pages([Page(None, text)])
    assert len(chunks) >= 2
    assert chunks[1].content.split()[0] in chunks[0].content.split()


def test_short_text_is_one_chunk():
    assert chunk_pages([Page(None, "short")]) == [Chunk(None, "short")]


def test_blank_text_produces_no_chunks():
    assert chunk_pages([Page(1, "   \n  ")]) == []


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("rrhh/a.pdf", True),
        ("a.docx", True),
        ("A.MD", True),
        ("notes.txt", True),
        ("sheet.xlsx", False),
        ("rrhh/~$a.docx", False),
        (".hidden/a.md", False),
        ("rrhh/.a.md", False),
    ],
)
def test_should_index(rel, expected):
    assert should_index(PurePosixPath(rel)) is expected


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("rrhh/a.pdf", "rrhh"),
        ("RRHH/sub/a.pdf", "rrhh"),
        (" Ventas /a.md", "ventas"),
        ("a.pdf", "general"),
    ],
)
def test_collection_for(rel, expected):
    assert collection_for(PurePosixPath(rel)) == expected


def test_read_windows_encoded_text_file(tmp_path):
    path = tmp_path / "notas.txt"
    path.write_bytes("Política de vacaciones: 23 días".encode("cp1252"))
    assert read_file(path) == [Page(None, "Política de vacaciones: 23 días")]
