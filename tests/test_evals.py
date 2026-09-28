from cka.ingest import ingest
from conftest import write_doc
from evals.run import evaluate

VACATION_TEXT = "Los empleados tienen 22 dias de vacaciones al año."


def test_evaluate_reports_hit_rate_and_score_range(conn, embedder, tmp_path):
    write_doc(tmp_path, "rrhh/vacaciones.md", VACATION_TEXT)
    write_doc(tmp_path, "general/horario.md", "La oficina abre a las 9 de la mañana.")
    ingest(conn, tmp_path, embedder)
    cases = [
        {"question": VACATION_TEXT, "expected": "rrhh/vacaciones.md"},
        {"question": "archivo que no existe", "expected": "rrhh/no-existe.md"},
        {"question": "zzz qqq", "expected": None},
    ]
    result = evaluate(conn, embedder, cases, top_k=1)
    assert result["hit_rate"] == 0.5
    assert result["min_correct_score"] > 0.99
    assert result["max_negative_score"] < result["min_correct_score"]
    assert result["misses"] == ["archivo que no existe"]
