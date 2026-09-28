# Company Knowledge Agent

Responde las preguntas de los empleados usando **solo los documentos de tu empresa**. Cada
respuesta cita el archivo y la página de donde sale, y dice "no lo sé" en vez de inventar.

- **Tus documentos tal como están:** PDF, Word (.docx), Markdown y TXT desde una carpeta.
  Funciona con las carpetas sincronizadas de OneDrive/SharePoint y Google Drive para escritorio.
- **Permisos por carpeta:** cada subcarpeta es una colección; cada usuario solo recibe
  respuestas de las colecciones que puede ver. El filtro se hace en la base de datos, nunca en
  la IA.
- **Usa tu propio modelo:** cualquier servidor compatible con OpenAI (NVIDIA NIM, vLLM,
  Ollama…) o Claude.
- **Para personas y sistemas:** un chat web y una API REST con API keys para la intranet, el
  sistema de tickets o bots.
- **Un solo comando:** `docker compose up`.

Hecho con Python, LangGraph, PostgreSQL + pgvector y FastAPI. [Read in English](README.md).

## Cómo funciona

```
documentos/           cka ingest                    Postgres + pgvector
├── general/   ──►  leer → trocear → embeddings ──►  fragmentos con su colección
├── rrhh/                                              │
└── finanzas/                                          ▼
                    pregunta ──► búsqueda (solo colecciones permitidas) ──► el LLM responde
                                                                             con citas [1] [2]
                                                                        ──► se validan las citas
```

## Inicio rápido

Requisitos: Docker con Docker Compose y acceso a un LLM.

```bash
git clone https://github.com/<tu-usuario>/company-knowledge-agent && cd company-knowledge-agent
cp .env.example .env
# edita .env: SECRET_KEY y las variables LLM_* (ver "Elegir el modelo")
docker compose up -d --build
docker compose run --rm ingest          # la primera vez descarga el modelo bge-m3 (~2 GB)
docker compose run --rm api cka create-user ana --collections rrhh
```

Abre http://localhost:8000 e inicia sesión como `ana`. El repo incluye documentos de ejemplo
ficticios en `examples/docs/` para probarlo al momento.

## Tus documentos y colecciones

Apunta `DOCS_HOST_DIR` del `.env` a la carpeta de documentos (mejor fuera de este repositorio):

```
documentos/
├── general/        → lo ve todo el mundo
├── rrhh/           → solo usuarios con la colección "rrhh"
├── finanzas/       → solo usuarios con la colección "finanzas"
└── archivo-suelto.pdf → los archivos de la raíz pertenecen a "general"
```

- Las colecciones no distinguen mayúsculas (`RRHH/` y `rrhh` son la misma).
- Se ignoran los archivos de bloqueo de Office (`~$archivo.docx`) y los archivos o carpetas
  ocultos.
- Los PDF escaneados sin texto se saltan con un aviso (todavía no hay OCR).
- Si la carpeta está vacía o alguna subcarpeta no se puede leer (por ejemplo, OneDrive sin
  montar o sin sincronizar), la carga se detiene sin borrar nada del índice. Para vaciar el
  índice a propósito, ejecuta `cka ingest --reindex`.
- **Pon un documento en una carpeta solo si todos los que tienen esa colección pueden leerlo.**

Vuelve a ejecutar `docker compose run --rm ingest` cuando cambien los documentos: solo se
procesan los archivos nuevos o modificados y se quitan del índice los borrados. Para que corra
cada noche:

```cron
0 2 * * * cd /opt/company-knowledge-agent && docker compose run --rm ingest >> ingest.log 2>&1
```

## Elegir el modelo

| Servidor | `.env` |
|---|---|
| NVIDIA NIM, vLLM o cualquier servidor compatible con OpenAI | `LLM_PROVIDER=openai`<br>`LLM_BASE_URL=http://tu-servidor:8000/v1`<br>`LLM_MODEL=qwen2.5-72b-instruct` |
| Ollama en el mismo equipo | `LLM_PROVIDER=openai`<br>`LLM_BASE_URL=http://host.docker.internal:11434/v1`<br>`LLM_MODEL=qwen2.5:14b` |
| Claude | `LLM_PROVIDER=anthropic`<br>`LLM_MODEL=claude-sonnet-5`<br>`LLM_API_KEY=sk-ant-...` |

La URL se usa desde dentro del contenedor: allí `localhost` es el propio contenedor. En Linux,
`host.docker.internal` necesita `extra_hosts: ["host.docker.internal:host-gateway"]` en el
servicio `api`.

**Embeddings.** Por defecto, el modelo multilingüe `BAAI/bge-m3` corre en CPU dentro del
contenedor. Si tu servidor también ofrece embeddings compatibles con OpenAI, define
`EMBEDDING_BASE_URL` y `EMBEDDING_MODEL`. Cambiar de modelo de embeddings obliga a reconstruir
el índice: `docker compose run --rm ingest cka ingest --reindex`.

## Usuarios y API keys

```bash
docker compose run --rm api cka create-user ana --collections rrhh,ventas
docker compose run --rm api cka set-collections ana --collections rrhh
docker compose run --rm api cka set-password ana
docker compose run --rm api cka delete-user ana
docker compose run --rm api cka create-api-key intranet --collections general   # se muestra una vez
docker compose run --rm api cka revoke-api-key intranet
```

Todos tienen siempre la colección `general`. Los cambios de permisos se aplican en la siguiente
petición del usuario.

Llamar a la API desde otro sistema:

```bash
curl http://localhost:8000/ask \
  -H "Authorization: Bearer cka_..." \
  -H "Content-Type: application/json" \
  -d '{"question": "¿Cuántos días de vacaciones tengo?"}'
```

```json
{
  "answer": "Tienes 23 días laborables de vacaciones al año [1].",
  "citations": [
    {"n": 1, "file": "rrhh/vacaciones.md", "page": null, "snippet": "Cada empleado tiene 23 días..."}
  ],
  "verified": true
}
```

`verified: false` significa que el modelo no citó sus fuentes ni siquiera tras un reintento:
trata esa respuesta con cuidado. Errores: `401` sin autenticar, `422` pregunta vacía o
demasiado larga, `429` demasiados intentos de login, `503` modelo o base de datos no
disponibles.

## Seguridad

- Sírvelo detrás del proxy HTTPS de tu empresa y pon `COOKIE_SECURE=true`. **No lo expongas
  directamente a internet.**
- Por defecto la API solo escucha en `127.0.0.1`. Pon `API_BIND=0.0.0.0` en el `.env` solo si
  un proxy en otra máquina necesita llegar a ella.
- Cambiar la contraseña de un usuario (`cka set-password`) cierra todas sus sesiones abiertas.
- Los logs guardan quién preguntó, cuándo, en qué colecciones y cuánto tardó; nunca la
  pregunta ni la respuesta.
- Las contraseñas se guardan con scrypt; las API keys, como hash SHA-256.
- Tras 5 intentos de login fallidos el usuario queda bloqueado 15 minutos. El contador está en
  memoria: se reinicia al reiniciar la API y no se comparte entre réplicas.

## Ajustar la calidad de la búsqueda

Pon preguntas reales de tu empresa en `evals/preguntas.yaml` y ejecuta:

```bash
uv run --env-file .env --extra local python evals/run.py
```

Te dice con qué frecuencia se encuentra el documento correcto y sugiere un rango para
`MIN_SCORE`.

## Desarrollo

```bash
uv sync                      # añade --extra local para correr el modelo de embeddings en local
docker compose up -d db      # crea también la base cka_test que usan los tests
uv run pytest
uv run ruff check .
uv run --env-file .env uvicorn cka.api:create_app --factory --reload
```

Notas de diseño: [docs/design.md](docs/design.md).

## Próximos pasos

- Inicio de sesión con Microsoft Entra ID / Google (OIDC)
- Conectores directos a SharePoint y Google Drive
- Búsqueda híbrida (vectores + texto completo)
- Descargar el documento original desde el chat
- Bot de Microsoft Teams

Las contribuciones son bienvenidas.

## Licencia

[Apache 2.0](LICENSE)
