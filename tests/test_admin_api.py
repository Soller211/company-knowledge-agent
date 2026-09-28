import logging
import os
from pathlib import PurePosixPath

import pytest
from fastapi.testclient import TestClient

from cka import admin, auth
from cka.api import create_app
from cka.ingest import INGEST_LOCK_ID, INTERRUPTED, mark_processing
from conftest import ScriptedLLM, make_pdf, make_settings

VACATION = "Los empleados tienen 22 dias de vacaciones al año.".encode()


@pytest.fixture
def docs_dir(tmp_path):
    path = tmp_path / "docs"
    path.mkdir()
    return path


@pytest.fixture
def users(conn):
    auth.create_user(conn, "laura", "contraseña-laura", [], is_admin=True)
    auth.create_user(conn, "ana", "contraseña-ana", ["rrhh"])
    return conn


def make_client(embedder, docs_dir, user="laura", llm=None, csrf=True, **settings):
    app = create_app(
        make_settings(docs_dir=str(docs_dir), **settings),
        llm=llm or ScriptedLLM(),
        embedder=embedder,
    )
    client = TestClient(app)
    if csrf:
        client.headers["X-Requested-With"] = "cka"
    if user:
        client.post("/login", json={"username": user, "password": f"contraseña-{user}"})
    return client


def wait_idle(client):
    # The ingest worker runs jobs in order, so an empty job finishes after every queued one.
    client.app.state.ingest_worker.submit(lambda: None).result(timeout=30)


def upload(client, name, content=VACATION, collection="rrhh", replace=False):
    data = {"collection": collection}
    if replace:
        data["replace"] = "true"
    response = client.post("/api/admin/documents", data=data, files={"file": (name, content)})
    wait_idle(client)
    return response


def documents(client):
    return {d["path"]: d for d in client.get("/api/admin/documents").json()}


ADMIN_CALLS = [
    ("get", "/api/admin/documents", {}),
    ("get", "/api/admin/collections", {}),
    ("post", "/api/admin/sync", {}),
    ("delete", "/api/admin/documents/rrhh/a.md", {}),
    (
        "post",
        "/api/admin/documents",
        {"data": {"collection": "rrhh"}, "files": {"file": ("a.md", b"x")}},
    ),
    ("get", "/api/admin/users", {}),
    ("post", "/api/admin/users", {"json": {"username": "x", "password": "12345678"}}),
    ("patch", "/api/admin/users/ana", {"json": {"is_admin": True}}),
    ("post", "/api/admin/users/ana/password", {"json": {"password": "12345678"}}),
    ("delete", "/api/admin/users/ana", {}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), ADMIN_CALLS)
def test_admin_routes_reject_non_admins(users, embedder, docs_dir, method, path, kwargs):
    anonymous = make_client(embedder, docs_dir, user=None)
    assert getattr(anonymous, method)(path, **kwargs).status_code == 401
    regular = make_client(embedder, docs_dir, user="ana")
    assert getattr(regular, method)(path, **kwargs).status_code == 403
    key = auth.create_api_key(users, "bot", ["rrhh"])
    by_key = make_client(embedder, docs_dir, user=None)
    headers = {"Authorization": f"Bearer {key}"}
    assert getattr(by_key, method)(path, headers=headers, **kwargs).status_code == 403


def test_me_reports_admin(users, embedder, docs_dir):
    assert make_client(embedder, docs_dir).get("/me").json()["is_admin"] is True
    assert make_client(embedder, docs_dir, user="ana").get("/me").json()["is_admin"] is False


def test_upload_is_processed_and_answerable(users, embedder, docs_dir):
    admin_client = make_client(embedder, docs_dir)
    response = upload(admin_client, "vacaciones.md")
    assert response.status_code == 202
    assert response.json() == {"path": "rrhh/vacaciones.md", "status": "processing"}
    assert (docs_dir / "rrhh" / "vacaciones.md").read_bytes() == VACATION
    assert documents(admin_client)["rrhh/vacaciones.md"]["status"] == "ready"
    llm = ScriptedLLM("Hay 22 dias [1].")
    ana = make_client(embedder, docs_dir, user="ana", llm=llm)
    body = ana.post("/ask", json={"question": "cuantos dias de vacaciones hay"}).json()
    assert body["citations"][0]["file"] == "rrhh/vacaciones.md"


@pytest.mark.parametrize(
    ("name", "collection", "message"),
    [
        ("tabla.xlsx", "rrhh", "Formato no soportado"),
        ("../x.md", "rrhh", "Nombre de archivo"),
        ("a\\b.md", "rrhh", "Nombre de archivo"),
        (".oculto.md", "rrhh", "Nombre de archivo"),
        ("~$x.docx", "rrhh", "Nombre de archivo"),
        ("x.md", "Recursos Humanos", "colección"),
    ],
)
def test_upload_rejections(users, embedder, docs_dir, name, collection, message):
    response = upload(make_client(embedder, docs_dir), name, collection=collection)
    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert not any(docs_dir.rglob("*.md"))


def test_upload_too_large(users, embedder, docs_dir):
    client = make_client(embedder, docs_dir, max_upload_mb=1)
    response = upload(client, "grande.md", b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert not (docs_dir / "rrhh" / "grande.md").exists()


def test_upload_replaces_existing_file(users, embedder, docs_dir):
    client = make_client(embedder, docs_dir)
    upload(client, "a.md", b"version uno")
    clash = upload(client, "a.md", b"version dos")
    assert clash.status_code == 409
    assert "Ya existe" in clash.json()["detail"]
    assert upload(client, "a.md", b"version dos", replace=True).status_code == 202
    contents = [r[0] for r in users.execute("SELECT content FROM chunks").fetchall()]
    assert contents == ["version dos"]


def test_broken_and_scanned_uploads_show_their_status(users, embedder, docs_dir, tmp_path):
    client = make_client(embedder, docs_dir)
    upload(client, "roto.pdf", b"no es un pdf")
    make_pdf(tmp_path / "scan.pdf", [""])
    upload(client, "scan.pdf", (tmp_path / "scan.pdf").read_bytes())
    docs = documents(client)
    assert docs["rrhh/roto.pdf"]["status"] == "error"
    assert docs["rrhh/roto.pdf"]["error"]
    assert docs["rrhh/scan.pdf"]["status"] == "empty"


def test_upload_goes_to_existing_uppercase_folder(users, embedder, docs_dir):
    (docs_dir / "RRHH").mkdir()
    response = upload(make_client(embedder, docs_dir), "a.md")
    assert response.json()["path"] == "RRHH/a.md"


def test_upload_when_docs_dir_is_missing(users, embedder, tmp_path):
    client = make_client(embedder, tmp_path / "no-existe")
    assert upload(client, "a.md").status_code == 503


def test_delete_document(users, embedder, docs_dir):
    client = make_client(embedder, docs_dir)
    upload(client, "a.md")
    assert client.delete("/api/admin/documents/rrhh/a.md").status_code == 204
    assert not (docs_dir / "rrhh" / "a.md").exists()
    assert documents(client) == {}
    assert client.delete("/api/admin/documents/rrhh/a.md").status_code == 404


def test_sync_indexes_folder_changes_and_refuses_concurrent_runs(users, embedder, docs_dir):
    (docs_dir / "general").mkdir()
    (docs_dir / "general" / "horario.md").write_text("La oficina abre a las 9.", encoding="utf-8")
    client = make_client(embedder, docs_dir)
    assert client.post("/api/admin/sync").status_code == 202
    wait_idle(client)
    assert documents(client)["general/horario.md"]["status"] == "ready"
    users.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK_ID,))  # e.g. the nightly ingest
    try:
        response = client.post("/api/admin/sync")
    finally:
        users.execute("SELECT pg_advisory_unlock(%s)", (INGEST_LOCK_ID,))
    assert response.status_code == 409


def test_collections(users, embedder, docs_dir):
    (docs_dir / "ventas").mkdir()
    client = make_client(embedder, docs_dir)
    upload(client, "a.md")
    assert client.get("/api/admin/collections").json() == ["general", "rrhh", "ventas"]


def test_processing_documents_become_interrupted_on_startup(users, embedder, docs_dir):
    mark_processing(users, PurePosixPath("rrhh/a.pdf"))
    client = make_client(embedder, docs_dir)
    doc = documents(client)["rrhh/a.pdf"]
    assert (doc["status"], doc["error"]) == ("error", INTERRUPTED)


def test_user_management(users, embedder, docs_dir):
    client = make_client(embedder, docs_dir)
    new = {"username": "Luis", "password": "clave-luis-1", "collections": ["finanzas"]}
    assert client.post("/api/admin/users", json=new).status_code == 201
    assert client.post("/api/admin/users", json=new).status_code == 409
    listed = {u["username"]: u for u in client.get("/api/admin/users").json()}
    assert listed["luis"] == {
        "username": "luis",
        "collections": ["finanzas", "general"],
        "is_admin": False,
    }
    response = client.patch(
        "/api/admin/users/luis", json={"collections": ["ventas"], "is_admin": True}
    )
    assert response.json() == {
        "username": "luis",
        "collections": ["general", "ventas"],
        "is_admin": True,
    }
    assert client.patch("/api/admin/users/nadie", json={"is_admin": True}).status_code == 404


def test_password_reset_signs_the_user_out(users, embedder, docs_dir):
    ana = make_client(embedder, docs_dir, user="ana")
    assert ana.get("/me").status_code == 200
    client = make_client(embedder, docs_dir)
    reset = client.post("/api/admin/users/ana/password", json={"password": "temporal-123"})
    assert reset.status_code == 204
    assert ana.get("/me").status_code == 401
    fresh = make_client(embedder, docs_dir, user=None)
    login = fresh.post("/login", json={"username": "ana", "password": "temporal-123"})
    assert login.status_code == 200
    short = client.post("/api/admin/users/ana/password", json={"password": "corta"})
    assert short.status_code == 422
    unknown = client.post("/api/admin/users/nadie/password", json={"password": "temporal-123"})
    assert unknown.status_code == 404


def test_delete_user(users, embedder, docs_dir):
    client = make_client(embedder, docs_dir)
    assert client.delete("/api/admin/users/ana").status_code == 204
    assert client.delete("/api/admin/users/ana").status_code == 404


def test_admin_cannot_demote_or_delete_themselves(users, embedder, docs_dir):
    client = make_client(embedder, docs_dir)
    assert client.patch("/api/admin/users/laura", json={"is_admin": False}).status_code == 409
    assert client.delete("/api/admin/users/laura").status_code == 409
    assert client.get("/me").json()["is_admin"] is True


def test_admin_actions_are_audited(users, embedder, docs_dir, caplog):
    caplog.set_level(logging.INFO, logger="cka.admin")
    client = make_client(embedder, docs_dir)
    upload(client, "a.md")
    assert "admin=laura action=upload target=rrhh/a.md" in caplog.text
    assert "22 dias" not in caplog.text


def test_mutations_require_the_panel_header(users, embedder, docs_dir):
    # A form auto-submitted from another site on the same domain cannot add this header.
    client = make_client(embedder, docs_dir, csrf=False)
    assert client.post("/api/admin/sync").status_code == 403
    assert upload(client, "a.md").status_code == 403
    assert client.get("/api/admin/documents").status_code == 200


def test_oversized_upload_is_rejected_from_its_declared_size(users, embedder, docs_dir):
    client = make_client(embedder, docs_dir, max_upload_mb=1)
    response = upload(client, "grande.md", b"x" * (3 * 1024 * 1024))
    assert response.status_code == 413
    assert "1 MB" in response.json()["detail"]


def test_delete_waits_for_a_running_ingest_instead_of_racing_it(users, embedder, docs_dir, monkeypatch):
    client = make_client(embedder, docs_dir)
    upload(client, "a.md")
    monkeypatch.setattr(admin, "DELETE_LOCK_TIMEOUT", "200ms")
    users.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK_ID,))
    try:
        response = client.delete("/api/admin/documents/rrhh/a.md")
    finally:
        users.execute("SELECT pg_advisory_unlock(%s)", (INGEST_LOCK_ID,))
    assert response.status_code == 409
    assert (docs_dir / "rrhh" / "a.md").exists()
    assert client.delete("/api/admin/documents/rrhh/a.md").status_code == 204


def test_long_filename_is_rejected(users, embedder, docs_dir):
    response = upload(make_client(embedder, docs_dir), "a" * 220 + ".md")
    assert response.status_code == 400
    assert not (docs_dir / "rrhh").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_docs_dir_gives_a_clear_error(users, embedder, docs_dir):
    client = make_client(embedder, docs_dir)
    docs_dir.chmod(0o555)
    try:
        response = upload(client, "a.md", collection="nueva")
    finally:
        docs_dir.chmod(0o755)
    assert response.status_code == 500
    assert "permisos" in response.json()["detail"]
    assert not (docs_dir / "nueva").exists()


def test_symlinked_collection_pointing_outside_is_rejected(users, embedder, docs_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (docs_dir / "rrhh").symlink_to(outside, target_is_directory=True)
    response = upload(make_client(embedder, docs_dir), "a.md")
    assert response.status_code == 400
    assert list(outside.iterdir()) == []


def test_delete_never_removes_files_outside_docs_dir(users, embedder, docs_dir, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("fuera", encoding="utf-8")
    (docs_dir / "rrhh").mkdir()
    (docs_dir / "rrhh" / "x.md").symlink_to(secret)
    users.execute("INSERT INTO documents (path, collection, sha256) VALUES ('rrhh/x.md', 'rrhh', 'h')")
    response = make_client(embedder, docs_dir).delete("/api/admin/documents/rrhh/x.md")
    assert response.status_code == 400
    assert secret.exists()
