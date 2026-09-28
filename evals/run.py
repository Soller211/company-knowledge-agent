"""Measure search quality against evals/preguntas.yaml and suggest a MIN_SCORE.

Usage: uv run --env-file .env --extra local python evals/run.py [questions.yaml]
Run `cka ingest` first so the documents are indexed.
"""

import sys
from pathlib import Path

import yaml

from cka import db
from cka.agent import search_chunks
from cka.config import Settings
from cka.embeddings import make_embedder


def evaluate(conn, embedder, cases: list[dict], top_k: int) -> dict:
    collections = [row[0] for row in conn.execute("SELECT DISTINCT collection FROM chunks")]
    positives = [case for case in cases if case["expected"]]
    found, correct_scores, negative_scores, misses = 0, [], [], []
    for case in cases:
        vector = embedder.embed([case["question"]])[0]
        hits = search_chunks(conn, vector, collections, top_k, min_score=-1.0)
        if not case["expected"]:
            if hits:
                negative_scores.append(hits[0].score)
            continue
        match = next((hit for hit in hits if hit.path == case["expected"]), None)
        if match:
            found += 1
            correct_scores.append(match.score)
        else:
            misses.append(case["question"])
    return {
        "hit_rate": found / len(positives) if positives else 0.0,
        "min_correct_score": min(correct_scores, default=None),
        "max_negative_score": max(negative_scores, default=None),
        "misses": misses,
    }


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("preguntas.yaml")
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings = Settings.from_env()
    embedder = make_embedder(settings)
    db.init_schema(settings.database_url, embedder.dim, embedder.model_name)
    with db.connect(settings.database_url) as conn:
        result = evaluate(conn, embedder, cases, settings.top_k)
    print(f"Acierto en top-{settings.top_k}: {result['hit_rate']:.0%}")
    print(f"Puntuación mínima de un acierto:        {result['min_correct_score']}")
    print(f"Puntuación máxima de una sin respuesta: {result['max_negative_score']}")
    print("Pon MIN_SCORE entre esos dos valores.")
    for question in result["misses"]:
        print(f"  ✗ no encontrado: {question}")


if __name__ == "__main__":
    main()
