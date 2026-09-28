import logging

import psycopg
import pytest
from fastapi.testclient import TestClient

from cka import auth, db
from cka.api import create_app
from cka.ingest import ingest
from conftest import BrokenLLM, ScriptedLLM, make_settings, write_doc


@pytest.fixture
def seeded(conn, embedder, tmp_path):
    write_doc(tmp_path, "rrhh/vacaciones.md", "Los empleados tienen 22 dias de vacaciones al año.")
    write_doc(
        tmp_path,
        "finanzas/presupuesto.md",
        "El presupuesto confidencial de finanzas es 5 millones.",
    )
    write_doc(tmp_path, "general/horario.md", "La oficina abre a las 9 de la mañana.")
    ingest(conn, tmp_path, embedder)
    auth.create_user(conn, "ana", "contraseña-ana", ["rrhh"])
    return conn


def client_for(embedder, llm):
    return TestClient(create_app(make_settings(), llm=llm, embedder=embedder))


def login(client, username="ana", password="contraseña-ana"):
    return client.post("/login", json={"username": username, "password": password})


def ask(client, question="cuantos dias de vacaciones hay", **kwargs):
    return client.post("/ask", json={"question": question}, **kwargs)


def raise_db_down(url):
    raise psycopg.OperationalError("db down")


def test_index_serves_the_chat_page(seeded, embedder):
    response = client_for(embedder, ScriptedLLM()).get("/")
    assert response.status_code == 200
    assert "Company Knowledge Agent" in response.text


def test_health(seeded, embedder, monkeypatch):
    client = client_for(embedder, ScriptedLLM())
    assert client.get("/health").json() == {"status": "ok"}
    monkeypatch.setattr(db, "connect", raise_db_down)
    assert client.get("/health").status_code == 503


def test_ask_requires_authentication(seeded, embedder):
    assert ask(client_for(embedder, ScriptedLLM())).status_code == 401


def test_wrong_password_is_rejected(seeded, embedder):
    assert login(client_for(embedder, ScriptedLLM()), password="mala").status_code == 401


def test_login_then_ask_returns_cited_answer(seeded, embedder):
    client = client_for(embedder, ScriptedLLM("Hay 22 dias [1]."))
    assert login(client).json() == {"user": "ana", "collections": ["general", "rrhh"]}
    assert client.get("/me").json()["user"] == "ana"
    body = ask(client).json()
    assert body["verified"] is True
    assert body["answer"] == "Hay 22 dias [1]."
    assert body["citations"][0]["file"] == "rrhh/vacaciones.md"


def test_user_never_gets_other_collections(seeded, embedder):
    llm = ScriptedLLM("NOT_FOUND")
    client = client_for(embedder, llm)
    login(client)
    body = ask(client, "presupuesto confidencial de finanzas").json()
    assert body["answer"] == make_settings().not_found_message
    assert "5 millones" not in llm.calls[0]


def test_api_key_only_sees_its_collections(seeded, embedder):
    key = auth.create_api_key(seeded, "intranet", ["finanzas"])
    llm = ScriptedLLM("NOT_FOUND")
    response = ask(
        client_for(embedder, llm),
        "presupuesto confidencial de finanzas",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200
    assert "5 millones" in llm.calls[0]
    assert "22 dias" not in llm.calls[0]


def test_revoked_api_key_is_rejected(seeded, embedder):
    key = auth.create_api_key(seeded, "intranet", ["finanzas"])
    auth.revoke_api_key(seeded, "intranet")
    response = ask(client_for(embedder, ScriptedLLM()), headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 401


def test_account_locks_after_five_failures(seeded, embedder):
    client = client_for(embedder, ScriptedLLM())
    for _ in range(5):
        assert login(client, password="mala").status_code == 401
    response = login(client)
    assert response.status_code == 429
    assert "15 minutos" in response.json()["detail"]


def test_llm_down_returns_503(seeded, embedder):
    client = client_for(embedder, BrokenLLM())
    login(client)
    response = ask(client)
    assert response.status_code == 503
    assert response.json()["detail"] == "El modelo no está disponible, intenta en un momento"


def test_db_down_on_ask_returns_503(seeded, embedder, monkeypatch):
    client = client_for(embedder, ScriptedLLM())
    login(client)
    monkeypatch.setattr(db, "connect", raise_db_down)
    assert ask(client).status_code == 503


@pytest.mark.parametrize("question", ["", "   \n ", "x" * 2001])
def test_blank_or_huge_question_is_rejected(seeded, embedder, question):
    llm = ScriptedLLM()
    client = client_for(embedder, llm)
    login(client)
    assert ask(client, question).status_code == 422
    assert llm.calls == []


def test_logout_ends_the_session(seeded, embedder):
    client = client_for(embedder, ScriptedLLM())
    login(client)
    client.post("/logout")
    assert client.get("/me").status_code == 401


def test_permission_changes_apply_to_open_sessions(seeded, embedder):
    client = client_for(embedder, ScriptedLLM())
    login(client)
    auth.set_collections(seeded, "ana", ["finanzas"])
    assert client.get("/me").json()["collections"] == ["finanzas", "general"]
    auth.delete_user(seeded, "ana")
    assert client.get("/me").status_code == 401


def test_logs_never_contain_the_question(seeded, embedder, caplog):
    caplog.set_level(logging.INFO, logger="cka.api")
    client = client_for(embedder, ScriptedLLM("Hay 22 dias [1]."))
    login(client)
    ask(client, "PREGUNTA-SECRETA-123 vacaciones")
    assert "ask principal=ana" in caplog.text
    assert "PREGUNTA-SECRETA-123" not in caplog.text
    assert "22 dias" not in caplog.text


def test_password_change_logs_out_open_sessions(seeded, embedder):
    client = client_for(embedder, ScriptedLLM())
    login(client)
    auth.set_password(seeded, "ana", "clave-nueva-123")
    assert client.get("/me").status_code == 401
