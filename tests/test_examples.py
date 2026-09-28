from pathlib import Path

from fastapi.testclient import TestClient

from cka import auth
from cka.api import create_app
from cka.ingest import ingest
from conftest import ScriptedLLM, make_settings

EXAMPLES = Path(__file__).parent.parent / "examples" / "docs"


def test_examples_end_to_end(conn, embedder):
    report = ingest(conn, EXAMPLES, embedder)
    assert report.errors == {}
    assert len(report.loaded) == 5
    auth.create_user(conn, "ana", "contraseña-ana", ["rrhh"])
    llm = ScriptedLLM("Tienes 23 días laborables de vacaciones [1].")
    client = TestClient(create_app(make_settings(), llm=llm, embedder=embedder))
    client.post("/login", json={"username": "ana", "password": "contraseña-ana"})
    response = client.post("/ask", json={"question": "¿Cuántos días de vacaciones tengo al año?"})
    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is True
    assert body["citations"][0]["file"].startswith(("rrhh/", "general/"))
    assert "rrhh/vacaciones.md" in llm.calls[0]
    assert "gastos-de-viaje" not in llm.calls[0]  # finanzas never reaches the model
