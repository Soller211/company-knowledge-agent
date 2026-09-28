# Company Knowledge Agent

Answer your employees' questions using **only your company's documents**. Every answer cites
the file and page it came from, and says "I don't know" instead of making things up.

- **Your documents as they are:** PDF, Word (.docx), Markdown and TXT from a folder. Works with
  OneDrive/SharePoint and Google Drive desktop sync folders.
- **Access control by folder:** each subfolder is a collection; users only get answers from the
  collections they may see. The filter runs in the database, never in the AI.
- **Bring your own model:** any OpenAI-compatible server (NVIDIA NIM, vLLM, Ollama…) or Claude.
- **For people and systems:** a web chat, and a REST API with API keys for your intranet,
  ticketing system or bots.
- **One command:** `docker compose up`.

Built with Python, LangGraph, PostgreSQL + pgvector and FastAPI. [Leer en español](README.es.md).

## How it works

```
documents/            cka ingest                    Postgres + pgvector
├── general/   ──►  read → chunk → embed  ──►  chunks tagged with their collection
├── rrhh/                                              │
└── finanzas/                                          ▼
                    question ──► search (only allowed collections) ──► LLM answers
                                                                       with [1] [2] citations
                                                                  ──► citations are checked
```

## Quick start

Requirements: Docker with Docker Compose, and access to an LLM.

```bash
git clone https://github.com/Soller211/company-knowledge-agent && cd company-knowledge-agent
cp .env.example .env
# edit .env: set SECRET_KEY and the LLM_* variables (see "Choosing the model")
docker compose up -d --build
docker compose run --rm ingest          # first run downloads the bge-m3 model (~2 GB)
docker compose run --rm api cka create-user ana --collections rrhh
```

Open http://localhost:8000 and log in as `ana`. The repo ships with fictional sample documents
in `examples/docs/` so you can try it right away.

## Your documents and collections

Point `DOCS_HOST_DIR` in `.env` to your documents folder (keep it outside this repository):

```
documents/
├── general/      → everyone can read it
├── rrhh/         → only users with the "rrhh" collection
├── finanzas/     → only users with the "finanzas" collection
└── loose-file.pdf → files at the top level belong to "general"
```

- Collection names are case-insensitive (`RRHH/` and `rrhh` are the same collection).
- Office lock files (`~$file.docx`) and hidden files/folders are ignored.
- Scanned PDFs without a text layer are skipped with a warning (no OCR yet).
- If the folder is empty or a subfolder can't be read (for example an unmounted or unsynced
  OneDrive folder), ingest stops without deleting anything from the index. To empty the index
  on purpose, run `cka ingest --reindex`.
- **Only put a document in a folder if everyone with that collection may read it.**

Re-run `docker compose run --rm ingest` whenever documents change: only new or modified files
are processed and deleted files are removed from the index. To run it every night:

```cron
0 2 * * * cd /opt/company-knowledge-agent && docker compose run --rm ingest >> ingest.log 2>&1
```

## Choosing the model

| Server | `.env` |
|---|---|
| NVIDIA NIM, vLLM or any OpenAI-compatible server | `LLM_PROVIDER=openai`<br>`LLM_BASE_URL=http://your-server:8000/v1`<br>`LLM_MODEL=qwen2.5-72b-instruct` |
| Ollama on the same machine | `LLM_PROVIDER=openai`<br>`LLM_BASE_URL=http://host.docker.internal:11434/v1`<br>`LLM_MODEL=qwen2.5:14b` |
| Claude | `LLM_PROVIDER=anthropic`<br>`LLM_MODEL=claude-sonnet-5`<br>`LLM_API_KEY=sk-ant-...` |

The URL is reached from inside the container: `localhost` there means the container itself.
On Linux, `host.docker.internal` needs `extra_hosts: ["host.docker.internal:host-gateway"]`
on the `api` service.

**Embeddings.** By default the multilingual `BAAI/bge-m3` model runs on CPU inside the
container. If your server also serves an OpenAI-compatible embeddings endpoint, set
`EMBEDDING_BASE_URL` and `EMBEDDING_MODEL`. Changing the embedding model requires rebuilding
the index: `docker compose run --rm ingest cka ingest --reindex`.

## Users and API keys

```bash
docker compose run --rm api cka create-user ana --collections rrhh,ventas
docker compose run --rm api cka set-collections ana --collections rrhh
docker compose run --rm api cka set-password ana
docker compose run --rm api cka delete-user ana
docker compose run --rm api cka create-api-key intranet --collections general   # shown once
docker compose run --rm api cka revoke-api-key intranet
```

Everyone always has the `general` collection. Permission changes apply on the user's next
request.

Calling the API from another system:

```bash
curl http://localhost:8000/ask \
  -H "Authorization: Bearer cka_..." \
  -H "Content-Type: application/json" \
  -d '{"question": "How many vacation days do I have?"}'
```

```json
{
  "answer": "You have 23 working days of vacation per year [1].",
  "citations": [
    {"n": 1, "file": "rrhh/vacaciones.md", "page": null, "snippet": "Cada empleado tiene 23 días..."}
  ],
  "verified": true
}
```

`verified: false` means the model did not cite its sources even after a retry: treat that
answer with care. Errors: `401` not authenticated, `422` empty or too long question, `429` too
many failed logins, `503` model or database unavailable.

## Security notes

- Serve it behind your company's HTTPS reverse proxy and set `COOKIE_SECURE=true`. **Do not
  expose it directly to the internet.**
- The API only listens on `127.0.0.1` by default. Set `API_BIND=0.0.0.0` in `.env` only when
  a reverse proxy on another machine must reach it.
- Changing a user's password (`cka set-password`) signs out all of their open sessions.
- Logs record who asked, when, in which collections and how long it took — never the question
  or the answer.
- Passwords are hashed with scrypt; API keys are stored as SHA-256 hashes.
- After 5 failed logins a user is locked for 15 minutes. The counter lives in memory: it resets
  on restart and is not shared between replicas.

## Tuning search quality

Put real questions from your company in `evals/preguntas.yaml`, then:

```bash
uv run --env-file .env --extra local python evals/run.py
```

It reports how often the right document is found and suggests a range for `MIN_SCORE`.

## Development

```bash
uv sync                      # add --extra local to run the embedding model locally
docker compose up -d db      # also creates the cka_test database used by the tests
uv run pytest
uv run ruff check .
uv run --env-file .env uvicorn cka.api:create_app --factory --reload
```

Design notes (in Spanish): [docs/design.md](docs/design.md).

## Roadmap

- Sign-in with Microsoft Entra ID / Google (OIDC)
- Direct SharePoint and Google Drive connectors
- Hybrid search (vector + full text)
- Downloading the original document from the chat
- Microsoft Teams bot

Contributions are welcome.

## License

[Apache 2.0](LICENSE)
