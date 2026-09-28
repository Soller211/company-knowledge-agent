import os
from pathlib import PurePosixPath

import pytest

from cka.agent import search_chunks
from cka.ingest import INTERRUPTED, ingest, ingest_file, mark_interrupted, mark_processing
from conftest import FakeEmbedder, make_pdf, write_doc


def chunk_rows(conn):
    return conn.execute(
        "SELECT path, collection, page, content FROM chunks ORDER BY path, id"
    ).fetchall()


def test_loads_files_into_their_collections(conn, embedder, tmp_path):
    write_doc(tmp_path, "rrhh/vacaciones.md", "Hay 22 dias de vacaciones.")
    # Different folder name on purpose: on macOS/Windows "RRHH" and "rrhh" are the same folder.
    write_doc(tmp_path, "Ventas/extra.md", "Carpeta en mayusculas.")
    write_doc(tmp_path, "suelto.txt", "Archivo en la raiz.")
    report = ingest(conn, tmp_path, embedder)
    assert sorted(report.loaded) == ["Ventas/extra.md", "rrhh/vacaciones.md", "suelto.txt"]
    assert {(r[0], r[1]) for r in chunk_rows(conn)} == {
        ("Ventas/extra.md", "ventas"),
        ("rrhh/vacaciones.md", "rrhh"),
        ("suelto.txt", "general"),
    }


def test_pdf_chunks_keep_page_numbers(conn, embedder, tmp_path):
    make_pdf(tmp_path / "rrhh" / "manual.pdf", ["first page", "second page"])
    ingest(conn, tmp_path, embedder)
    assert [(r[2], r[3]) for r in chunk_rows(conn)] == [(1, "first page"), (2, "second page")]


def test_second_run_skips_unchanged_files(conn, embedder, tmp_path):
    write_doc(tmp_path, "a.md", "contenido")
    ingest(conn, tmp_path, embedder)
    calls = embedder.calls
    report = ingest(conn, tmp_path, embedder)
    assert report.unchanged == 1
    assert report.loaded == []
    assert embedder.calls == calls


def test_modified_file_replaces_old_chunks(conn, embedder, tmp_path):
    path = write_doc(tmp_path, "a.md", "version uno")
    ingest(conn, tmp_path, embedder)
    path.write_text("version dos", encoding="utf-8")
    report = ingest(conn, tmp_path, embedder)
    assert report.loaded == ["a.md"]
    assert [r[3] for r in chunk_rows(conn)] == ["version dos"]


def test_deleted_file_is_removed(conn, embedder, tmp_path):
    path = write_doc(tmp_path, "a.md", "se va a borrar")
    write_doc(tmp_path, "b.md", "se queda")
    ingest(conn, tmp_path, embedder)
    path.unlink()
    report = ingest(conn, tmp_path, embedder)
    assert report.removed == ["a.md"]
    assert [r[0] for r in chunk_rows(conn)] == ["b.md"]
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


def test_file_moved_between_collections_follows_new_folder(conn, embedder, tmp_path):
    path = write_doc(tmp_path, "rrhh/nominas.md", "Tabla salarial confidencial.")
    ingest(conn, tmp_path, embedder)
    target = tmp_path / "general" / "nominas.md"
    target.parent.mkdir()
    path.rename(target)
    report = ingest(conn, tmp_path, embedder)
    assert report.loaded == ["general/nominas.md"]
    assert report.removed == ["rrhh/nominas.md"]
    assert {(r[0], r[1]) for r in chunk_rows(conn)} == {("general/nominas.md", "general")}


def test_unreadable_file_is_reported_and_retried(conn, embedder, tmp_path):
    (tmp_path / "roto.pdf").write_bytes(b"esto no es un pdf")
    write_doc(tmp_path, "ok.md", "contenido valido")
    report = ingest(conn, tmp_path, embedder)
    assert "roto.pdf" in report.errors
    assert report.loaded == ["ok.md"]
    assert "roto.pdf" in report.summary()
    again = ingest(conn, tmp_path, embedder)
    assert "roto.pdf" in again.errors  # not marked as processed, so it is retried


def test_embedding_failure_keeps_previous_version(conn, tmp_path):
    class FailingEmbedder(FakeEmbedder):
        def embed(self, texts):
            if any("ROMPER" in t for t in texts):
                raise RuntimeError("embedding server down")
            return super().embed(texts)

    embedder = FailingEmbedder()
    path = write_doc(tmp_path, "a.md", "version uno")
    ingest(conn, tmp_path, embedder)
    path.write_text("version dos ROMPER", encoding="utf-8")
    report = ingest(conn, tmp_path, embedder)
    assert "a.md" in report.errors
    assert [r[3] for r in chunk_rows(conn)] == ["version uno"]
    path.write_text("version tres", encoding="utf-8")
    ingest(conn, tmp_path, embedder)
    assert [r[3] for r in chunk_rows(conn)] == ["version tres"]


def test_scanned_pdf_is_recorded_as_empty(conn, embedder, tmp_path):
    make_pdf(tmp_path / "escaneado.pdf", ["", ""])
    report = ingest(conn, tmp_path, embedder)
    assert report.empty == ["escaneado.pdf"]
    assert chunk_rows(conn) == []
    assert ingest(conn, tmp_path, embedder).unchanged == 1


def test_office_lock_and_hidden_files_are_ignored(conn, embedder, tmp_path):
    (tmp_path / "rrhh").mkdir()
    (tmp_path / "rrhh" / "~$informe.docx").write_bytes(b"\x00lock")
    write_doc(tmp_path, ".oculto/x.md", "no indexar")
    write_doc(tmp_path, "rrhh/informe.md", "contenido real")
    report = ingest(conn, tmp_path, embedder)
    assert report.loaded == ["rrhh/informe.md"]
    assert report.errors == {}


def test_missing_docs_dir_raises_instead_of_wiping_index(conn, embedder, tmp_path):
    write_doc(tmp_path, "a.md", "no me borres")
    ingest(conn, tmp_path, embedder)
    with pytest.raises(FileNotFoundError):
        ingest(conn, tmp_path / "no-existe", embedder)
    assert len(chunk_rows(conn)) == 1


def test_empty_docs_dir_does_not_wipe_index(conn, embedder, tmp_path):
    # An unmounted OneDrive folder or a bind mount Docker created empty looks like this.
    write_doc(tmp_path / "docs", "a.md", "no me borres")
    ingest(conn, tmp_path / "docs", embedder)
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError, match="no contiene documentos"):
        ingest(conn, tmp_path / "empty", embedder)
    assert len(chunk_rows(conn)) == 1


def test_first_ingest_of_empty_dir_is_fine(conn, embedder, tmp_path):
    assert ingest(conn, tmp_path, embedder).loaded == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_subfolder_keeps_its_documents(conn, embedder, tmp_path):
    write_doc(tmp_path, "rrhh/a.md", "contenido de rrhh")
    write_doc(tmp_path, "general/b.md", "contenido general")
    ingest(conn, tmp_path, embedder)
    (tmp_path / "rrhh").chmod(0)
    try:
        report = ingest(conn, tmp_path, embedder)
    finally:
        (tmp_path / "rrhh").chmod(0o755)
    assert "rrhh" in report.errors
    assert report.removed == []
    assert {r[0] for r in chunk_rows(conn)} == {"general/b.md", "rrhh/a.md"}


def test_file_moved_into_restricted_collection_disappears_from_general(conn, embedder, tmp_path):
    path = write_doc(tmp_path, "general/nominas.md", "Tabla salarial confidencial.")
    write_doc(tmp_path, "general/otro.md", "Horario de oficina.")
    ingest(conn, tmp_path, embedder)
    (tmp_path / "rrhh").mkdir()
    path.rename(tmp_path / "rrhh" / "nominas.md")
    ingest(conn, tmp_path, embedder)
    vector = embedder.embed(["Tabla salarial confidencial."])[0]
    visible = {h.path for h in search_chunks(conn, vector, ["general"], 10, -1.0)}
    assert visible == {"general/otro.md"}


def doc_status(conn):
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT path, status, error FROM documents").fetchall()
    }


def test_statuses_are_recorded(conn, embedder, tmp_path):
    write_doc(tmp_path, "ok.md", "contenido")
    make_pdf(tmp_path / "escaneado.pdf", [""])
    (tmp_path / "roto.pdf").write_bytes(b"no es un pdf")
    ingest(conn, tmp_path, embedder)
    statuses = doc_status(conn)
    assert statuses["ok.md"] == ("ready", None)
    assert statuses["escaneado.pdf"] == ("empty", None)
    assert statuses["roto.pdf"][0] == "error"
    assert statuses["roto.pdf"][1]


def test_error_is_cleared_when_the_file_is_fixed(conn, embedder, tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"no es un pdf")
    write_doc(tmp_path, "otro.md", "contenido")
    ingest(conn, tmp_path, embedder)
    make_pdf(tmp_path / "doc.pdf", ["ahora si tiene texto"])
    ingest(conn, tmp_path, embedder)
    assert doc_status(conn)["doc.pdf"] == ("ready", None)


def test_failed_update_keeps_old_chunks_and_shows_error(conn, tmp_path):
    class FailingEmbedder(FakeEmbedder):
        def embed(self, texts):
            if any("ROMPER" in t for t in texts):
                raise RuntimeError("embedding server down")
            return super().embed(texts)

    embedder = FailingEmbedder()
    path = write_doc(tmp_path, "a.md", "version uno")
    ingest(conn, tmp_path, embedder)
    path.write_text("version dos ROMPER", encoding="utf-8")
    ingest(conn, tmp_path, embedder)
    assert doc_status(conn)["a.md"] == ("error", "embedding server down")
    assert [r[3] for r in chunk_rows(conn)] == ["version uno"]


def test_ingest_file_indexes_one_file(conn, embedder, tmp_path):
    write_doc(tmp_path, "rrhh/a.md", "contenido de rrhh")
    write_doc(tmp_path, "rrhh/b.md", "no se toca")
    assert ingest_file(conn, tmp_path, PurePosixPath("rrhh/a.md"), embedder) == ("ready", None)
    assert [(r[0], r[1]) for r in chunk_rows(conn)] == [("rrhh/a.md", "rrhh")]


def test_mark_processing_then_interrupted(conn, tmp_path):
    mark_processing(conn, PurePosixPath("rrhh/nuevo.pdf"))
    row = conn.execute("SELECT collection, status, sha256 FROM documents").fetchone()
    assert row == ("rrhh", "processing", None)
    assert mark_interrupted(conn) == 1
    assert doc_status(conn)["rrhh/nuevo.pdf"] == ("error", INTERRUPTED)


def test_ingest_file_skips_a_file_deleted_while_queued(conn, embedder, tmp_path):
    assert ingest_file(conn, tmp_path, PurePosixPath("rrhh/borrado.md"), embedder) == ("missing", None)
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
