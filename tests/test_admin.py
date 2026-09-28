import io

import pytest

from cka import admin
from cka.ingest import ingest
from conftest import write_doc


@pytest.mark.parametrize(
    ("name", "expected"),
    [("rrhh", "rrhh"), (" RRHH ", "rrhh"), ("atencion-cliente_2", "atencion-cliente_2")],
)
def test_valid_collections(name, expected):
    assert admin.validate_collection(name) == expected


@pytest.mark.parametrize(
    "name", ["", "recursos humanos", "-rrhh", "rrhh/x", "..", "a" * 65, "ñandu"]
)
def test_invalid_collections(name):
    with pytest.raises(admin.InvalidUpload, match="colección"):
        admin.validate_collection(name)


@pytest.mark.parametrize("name", ["informe.pdf", "Política 2026.docx", "notas.MD", "a.txt"])
def test_valid_filenames(name):
    assert admin.validate_filename(name) == name


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../x.pdf", "a/b.pdf", "a\\b.pdf", ".oculto.pdf", "~$x.docx", "x\x00.pdf"],
)
def test_dangerous_filenames(name):
    with pytest.raises(admin.InvalidUpload, match="Nombre de archivo"):
        admin.validate_filename(name)


def test_unsupported_format():
    with pytest.raises(admin.InvalidUpload, match="Formato no soportado"):
        admin.validate_filename("tabla.xlsx")


def test_save_upload_writes_atomically(tmp_path):
    rel = admin.save_upload(tmp_path, "rrhh", "a.md", io.BytesIO(b"hola"), max_bytes=100)
    assert str(rel) == "rrhh/a.md"
    assert (tmp_path / "rrhh" / "a.md").read_bytes() == b"hola"
    assert [p.name for p in (tmp_path / "rrhh").iterdir()] == ["a.md"]


def test_save_upload_reuses_existing_uppercase_folder(tmp_path):
    (tmp_path / "RRHH").mkdir()
    rel = admin.save_upload(tmp_path, "rrhh", "a.md", io.BytesIO(b"hola"), max_bytes=100)
    assert str(rel) == "RRHH/a.md"
    assert not (tmp_path / "rrhh").exists() or (tmp_path / "rrhh").samefile(tmp_path / "RRHH")


def test_save_upload_replaces_existing_file(tmp_path):
    admin.save_upload(tmp_path, "rrhh", "a.md", io.BytesIO(b"uno"), max_bytes=100)
    admin.save_upload(tmp_path, "rrhh", "a.md", io.BytesIO(b"dos"), max_bytes=100, replace=True)
    assert (tmp_path / "rrhh" / "a.md").read_bytes() == b"dos"


def test_save_upload_rejects_too_large_without_leftovers(tmp_path):
    (tmp_path / "rrhh").mkdir()
    with pytest.raises(admin.UploadTooLarge):
        admin.save_upload(tmp_path, "rrhh", "a.md", io.BytesIO(b"x" * 101), max_bytes=100)
    assert list((tmp_path / "rrhh").iterdir()) == []


def test_delete_document_removes_file_row_and_chunks(conn, embedder, tmp_path):
    write_doc(tmp_path, "rrhh/a.md", "contenido")
    write_doc(tmp_path, "rrhh/b.md", "otro")
    ingest(conn, tmp_path, embedder)
    admin.delete_document(conn, tmp_path, "rrhh/a.md")
    assert not (tmp_path / "rrhh" / "a.md").exists()
    assert [d["path"] for d in admin.list_documents(conn)] == ["rrhh/b.md"]
    assert conn.execute("SELECT count(*) FROM chunks WHERE path = 'rrhh/a.md'").fetchone()[0] == 0
    with pytest.raises(LookupError):
        admin.delete_document(conn, tmp_path, "rrhh/a.md")


def test_list_documents_and_collections(conn, embedder, tmp_path):
    write_doc(tmp_path, "rrhh/a.md", "contenido")
    (tmp_path / "Ventas").mkdir()
    (tmp_path / ".oculta").mkdir()
    (tmp_path / "Recursos Humanos").mkdir()
    ingest(conn, tmp_path, embedder)
    docs = admin.list_documents(conn)
    assert [(d["path"], d["collection"], d["status"], d["error"]) for d in docs] == [
        ("rrhh/a.md", "rrhh", "ready", None)
    ]
    assert "T" in docs[0]["updated_at"]
    assert admin.list_collections(conn, tmp_path) == ["general", "rrhh", "ventas"]


def test_existing_file_needs_replace(tmp_path):
    admin.save_upload(tmp_path, "rrhh", "a.md", io.BytesIO(b"uno"), max_bytes=100)
    with pytest.raises(admin.UploadExists):
        admin.save_upload(tmp_path, "rrhh", "a.md", io.BytesIO(b"dos"), max_bytes=100)
    assert (tmp_path / "rrhh" / "a.md").read_bytes() == b"uno"


def test_failed_write_removes_the_new_folder(tmp_path):
    class Broken:
        def read(self, n):
            raise OSError("disk full")

    with pytest.raises(OSError):
        admin.save_upload(tmp_path, "nueva", "a.md", Broken(), max_bytes=100)
    assert not (tmp_path / "nueva").exists()


def test_filename_length_is_capped():
    with pytest.raises(admin.InvalidUpload, match="Nombre de archivo"):
        admin.validate_filename("a" * 220 + ".md")
