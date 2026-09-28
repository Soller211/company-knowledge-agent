"""Web API: login, questions and the chat page."""

import logging
import threading
import time
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from cka import agent, auth, db
from cka.admin_api import build_admin_router
from cka.config import Settings
from cka.embeddings import make_embedder
from cka.ingest import mark_interrupted
from cka.llm import make_llm

log = logging.getLogger("cka.api")
STATIC_DIR = Path(__file__).parent / "static"
SESSION_MAX_AGE = 8 * 60 * 60
MAX_QUESTION_CHARS = 2000


class LoginIn(BaseModel):
    username: str
    password: str


class AskIn(BaseModel):
    question: str = Field(max_length=MAX_QUESTION_CHARS)

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("La pregunta está vacía")
        return value


def create_app(settings: Settings | None = None, *, llm=None, embedder=None) -> FastAPI:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = settings or Settings.from_env()
    embedder = embedder or make_embedder(settings)
    llm = llm or make_llm(settings)
    db.init_schema(settings.database_url, embedder.dim, embedder.model_name)
    with db.connect(settings.database_url) as conn:
        interrupted = mark_interrupted(conn)
    if interrupted:
        log.warning("%d documentos quedaron a medias por un reinicio", interrupted)
    ingest_lock = threading.Lock()
    throttle = auth.LoginThrottle()

    app = FastAPI(title="Company Knowledge Agent")
    app.state.ingest_lock = ingest_lock
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    # ponytail: one connection per request, no pool; add psycopg_pool if load grows.
    def get_conn():
        conn = db.connect(settings.database_url)
        try:
            yield conn
        finally:
            conn.close()

    def require_principal(request: Request, conn=Depends(get_conn)) -> auth.Principal:
        principal = auth.get_principal(request, conn)
        if principal is None:
            raise HTTPException(401, "No autenticado")
        return principal

    @app.exception_handler(psycopg.OperationalError)
    def database_unavailable(request: Request, exc: psycopg.OperationalError):
        log.error("Base de datos no disponible: %s", exc)
        return JSONResponse(
            {"detail": "La base de datos no está disponible, intenta en un momento"},
            status_code=503,
        )

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/documents", include_in_schema=False)
    def documents_page():
        return FileResponse(STATIC_DIR / "documents.html")

    @app.get("/users", include_in_schema=False)
    def users_page():
        return FileResponse(STATIC_DIR / "users.html")

    @app.get("/health")
    def health():
        try:
            with db.connect(settings.database_url) as conn:
                conn.execute("SELECT 1")
        except psycopg.Error:
            return JSONResponse({"status": "db_unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.post("/login")
    def login(body: LoginIn, request: Request, conn=Depends(get_conn)):
        try:
            principal = auth.authenticate(conn, body.username, body.password, throttle)
        except auth.AccountLocked:
            raise HTTPException(
                429, "Demasiados intentos fallidos. Intenta de nuevo en 15 minutos."
            ) from None
        if principal is None:
            raise HTTPException(401, "Usuario o contraseña incorrectos")
        auth.start_session(conn, request.session, principal.name)
        return {"user": principal.name, "collections": list(principal.collections)}

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/me")
    def me(principal: auth.Principal = Depends(require_principal)):
        return {
            "user": principal.name,
            "collections": list(principal.collections),
            "is_admin": principal.is_admin,
        }

    @app.post("/ask")
    def ask(
        body: AskIn,
        principal: auth.Principal = Depends(require_principal),
        conn=Depends(get_conn),
    ):
        started = time.monotonic()
        try:
            result = agent.ask(
                body.question,
                principal.collections,
                conn=conn,
                llm=llm,
                embedder=embedder,
                settings=settings,
            )
        except agent.LLMUnavailable as exc:
            log.error("LLM no disponible: %s", exc)
            raise HTTPException(
                503, "El modelo no está disponible, intenta en un momento"
            ) from None
        # Never log the question or answer text: they may contain sensitive information.
        log.info(
            "ask principal=%s collections=%s citations=%d verified=%s latency_ms=%d",
            principal.name,
            ",".join(principal.collections),
            len(result["citations"]),
            result["verified"],
            (time.monotonic() - started) * 1000,
        )
        return result

    app.include_router(
        build_admin_router(settings, embedder, get_conn, require_principal, ingest_lock)
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
