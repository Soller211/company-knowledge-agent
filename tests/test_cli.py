import pytest

from cka import auth, cli, db
from conftest import FakeEmbedder, write_doc


@pytest.fixture
def env(monkeypatch, db_url, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DOCS_DIR", str(docs))
    monkeypatch.setattr(cli, "make_embedder", lambda settings: FakeEmbedder())
    return docs


def type_passwords(monkeypatch, *values):
    answers = iter(values)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))


def connect(db_url):
    return db.connect(db_url)


def test_create_user_and_login(env, db_url, monkeypatch):
    type_passwords(monkeypatch, "contraseña-ana", "contraseña-ana")
    assert cli.main(["create-user", "ana", "--collections", "rrhh,ventas"]) == 0
    with connect(db_url) as conn:
        principal = auth.authenticate(conn, "ana", "contraseña-ana", auth.LoginThrottle())
    assert principal.collections == ("general", "rrhh", "ventas")


def test_create_user_rejects_short_or_mismatched_passwords(env, monkeypatch):
    type_passwords(monkeypatch, "corta")
    with pytest.raises(SystemExit, match="8 caracteres"):
        cli.main(["create-user", "ana"])
    type_passwords(monkeypatch, "contraseña-ana", "otra-cosa-distinta")
    with pytest.raises(SystemExit, match="no coinciden"):
        cli.main(["create-user", "ana"])


def test_duplicate_user_fails(env, monkeypatch):
    type_passwords(monkeypatch, *["contraseña-ana"] * 4)
    assert cli.main(["create-user", "ana"]) == 0
    assert cli.main(["create-user", "ana"]) == 1


def test_set_collections_and_delete_user(env, db_url, monkeypatch):
    type_passwords(monkeypatch, "contraseña-ana", "contraseña-ana")
    cli.main(["create-user", "ana"])
    assert cli.main(["set-collections", "ana", "--collections", "finanzas"]) == 0
    with connect(db_url) as conn:
        collections = conn.execute("SELECT collections FROM users").fetchone()[0]
    assert collections == ["finanzas", "general"]
    assert cli.main(["delete-user", "ana"]) == 0
    assert cli.main(["delete-user", "ana"]) == 1
    assert cli.main(["set-collections", "ana", "--collections", "x"]) == 1


def test_set_password(env, db_url, monkeypatch):
    type_passwords(
        monkeypatch, "contraseña-ana", "contraseña-ana", "nueva-clave-1", "nueva-clave-1"
    )
    cli.main(["create-user", "ana"])
    assert cli.main(["set-password", "ana"]) == 0
    with connect(db_url) as conn:
        assert auth.authenticate(conn, "ana", "nueva-clave-1", auth.LoginThrottle())


def test_api_key_lifecycle(env, db_url, capsys):
    assert cli.main(["create-api-key", "intranet", "--collections", "finanzas"]) == 0
    key = next(line for line in capsys.readouterr().out.split() if line.startswith("cka_"))
    with connect(db_url) as conn:
        assert auth.principal_from_api_key(conn, key).name == "api:intranet"
    assert cli.main(["revoke-api-key", "intranet"]) == 0
    with connect(db_url) as conn:
        assert auth.principal_from_api_key(conn, key) is None
    assert cli.main(["revoke-api-key", "intranet"]) == 1


def test_ingest_prints_summary_and_exit_code(env, capsys):
    write_doc(env, "rrhh/a.md", "contenido")
    assert cli.main(["ingest"]) == 0
    assert "1 cargados" in capsys.readouterr().out
    (env / "roto.pdf").write_bytes(b"no es pdf")
    assert cli.main(["ingest"]) == 1
    assert "roto.pdf" in capsys.readouterr().out


def test_ingest_missing_docs_dir_fails(env, monkeypatch, capsys):
    monkeypatch.setenv("DOCS_DIR", str(env / "no-existe"))
    assert cli.main(["ingest"]) == 1
    assert "DOCS_DIR" in capsys.readouterr().err


def test_ingest_with_changed_model_requires_reindex(env, db_url, capsys):
    db.init_schema(db_url, 16, "otro-modelo")
    write_doc(env, "a.md", "contenido")
    assert cli.main(["ingest"]) == 1
    assert "--reindex" in capsys.readouterr().err
    assert cli.main(["ingest", "--reindex"]) == 0
