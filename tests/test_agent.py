import pytest

from cka import agent
from cka.ingest import ingest
from conftest import BrokenLLM, ScriptedLLM, make_settings, write_doc

QUESTION = "cuantos dias de vacaciones hay"
VACATION_TEXT = "Los empleados tienen 22 dias de vacaciones al año."


@pytest.fixture
def docs(conn, embedder, tmp_path):
    write_doc(tmp_path, "rrhh/vacaciones.md", VACATION_TEXT)
    write_doc(
        tmp_path,
        "finanzas/presupuesto.md",
        "El presupuesto confidencial de vacaciones de finanzas es 5 millones.",
    )
    write_doc(tmp_path, "general/horario.md", "La oficina abre a las 9 de la mañana.")
    ingest(conn, tmp_path, embedder)


def run(conn, embedder, llm, question=QUESTION, collections=("general", "rrhh"), **overrides):
    return agent.ask(
        question,
        collections,
        conn=conn,
        llm=llm,
        embedder=embedder,
        settings=make_settings(**overrides),
    )


def test_search_never_returns_other_collections(conn, embedder, docs):
    vector = embedder.embed(["presupuesto confidencial de finanzas vacaciones"])[0]
    hits = agent.search_chunks(conn, vector, ["general", "rrhh"], top_k=10, min_score=-1.0)
    assert hits
    assert all(not h.path.startswith("finanzas/") for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_search_applies_min_score(conn, embedder, docs):
    vector = embedder.embed([VACATION_TEXT])[0]
    hits = agent.search_chunks(conn, vector, ["general", "rrhh"], top_k=10, min_score=0.99)
    assert [h.path for h in hits] == ["rrhh/vacaciones.md"]


def test_validate_citations():
    hits = [agent.Hit("a.md", None, "texto a", 0.9), agent.Hit("b.pdf", 3, "texto b", 0.8)]
    text, citations = agent.validate_citations("Uno [1]. Dos [2][7]. Tres [1, 2, 9].", hits)
    assert text == "Uno [1]. Dos [2]. Tres [1][2]."
    assert citations == [
        {"n": 1, "file": "a.md", "page": None, "snippet": "texto a"},
        {"n": 2, "file": "b.pdf", "page": 3, "snippet": "texto b"},
    ]


def test_answer_with_valid_citation(conn, embedder, docs):
    llm = ScriptedLLM("Hay 22 dias [1].")
    result = run(conn, embedder, llm)
    assert result["verified"] is True
    assert result["answer"] == "Hay 22 dias [1]."
    assert result["citations"][0]["file"] == "rrhh/vacaciones.md"
    assert len(llm.calls) == 1
    assert "(rrhh/vacaciones.md)" in llm.calls[0]


def test_llm_never_sees_other_collections(conn, embedder, docs):
    llm = ScriptedLLM(agent.NOT_FOUND_TOKEN)
    run(conn, embedder, llm, question="presupuesto confidencial de finanzas")
    assert "5 millones" not in llm.calls[0]


def test_no_accessible_chunks_skips_the_llm(conn, embedder, docs):
    llm = ScriptedLLM()
    result = run(conn, embedder, llm, collections=("ventas",))
    assert result == {
        "answer": make_settings().not_found_message,
        "citations": [],
        "verified": True,
    }
    assert llm.calls == []


def test_not_found_token_becomes_configured_message(conn, embedder, docs):
    result = run(conn, embedder, ScriptedLLM("NOT_FOUND."), not_found_message="Sin datos.")
    assert result == {"answer": "Sin datos.", "citations": [], "verified": True}


def test_retries_once_when_answer_has_no_citations(conn, embedder, docs):
    llm = ScriptedLLM("Hay 22 dias.", "Hay 22 dias [1].")
    result = run(conn, embedder, llm)
    assert result["verified"] is True
    assert len(llm.calls) == 2
    assert agent.RETRY_REMINDER in llm.calls[1]


def test_unverified_after_second_failure(conn, embedder, docs):
    llm = ScriptedLLM("Hay 22 dias.", "Siguen siendo 22 dias [9]")
    result = run(conn, embedder, llm)
    assert result == {"answer": "Siguen siendo 22 dias", "citations": [], "verified": False}
    assert len(llm.calls) == 2


def test_llm_failure_raises_llm_unavailable(conn, embedder, docs):
    with pytest.raises(agent.LLMUnavailable):
        run(conn, embedder, BrokenLLM())
