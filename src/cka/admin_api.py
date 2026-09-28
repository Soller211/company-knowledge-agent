"""Admin API for the web panel: documents, folder sync and users. Admins only."""

import logging
from concurrent.futures import Executor
from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field

from cka import admin, auth, db
from cka.config import Settings
from cka.embeddings import Embedder
from cka.ingest import (
    collection_for,
    ingest,
    ingest_busy,
    ingest_file,
    ingest_lock,
    mark_processing,
)

log = logging.getLogger("cka.admin")
MIN_PASSWORD_CHARS = 8
PANEL_HEADER = "x-requested-with"
PANEL_HEADER_VALUE = "cka"
BUSY_MESSAGE = "Hay una carga de documentos en curso, inténtalo en un momento"


class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=MIN_PASSWORD_CHARS)
    collections: list[str] = []
    is_admin: bool = False


class UserUpdate(BaseModel):
    collections: list[str] | None = None
    is_admin: bool | None = None


class NewPassword(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_CHARS)


def build_admin_router(
    settings: Settings,
    embedder: Embedder,
    get_conn,
    require_principal,
    worker: Executor,
) -> APIRouter:
    docs_dir = Path(settings.docs_dir)

    def require_panel_header(request: Request) -> None:
        # Forms auto-submitted from another site (even a sibling subdomain, which SameSite=Lax
        # does not block) cannot set custom headers, so this stops CSRF on uploads and actions.
        unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
        if unsafe and request.headers.get(PANEL_HEADER) != PANEL_HEADER_VALUE:
            raise HTTPException(403, "Petición no permitida")

    router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_panel_header)])

    def require_admin(principal: auth.Principal = Depends(require_principal)) -> auth.Principal:
        if not principal.is_admin:
            raise HTTPException(403, "Solo los administradores pueden hacer esto")
        return principal

    def require_can_manage(principal: auth.Principal, collection: str) -> None:
        if not auth.can_manage(principal, collection):
            raise HTTPException(403, "No puedes gestionar documentos de esta colección")

    def audit(principal: auth.Principal, action: str, target) -> None:
        log.info("admin=%s action=%s target=%s", principal.name, action, target)

    # Jobs run one at a time on the single ingest worker, so a burst of uploads queues there
    # instead of parking threads from the request pool. The advisory lock also serializes them
    # with `cka ingest` running from cron in another container.
    def process_upload(rel: PurePosixPath) -> None:
        try:
            with db.connect(settings.database_url) as conn, ingest_lock(conn):
                ingest_file(conn, docs_dir, rel, embedder)
        except Exception as exc:  # noqa: BLE001 - worker job: nobody else would see the error
            log.error("No se pudo procesar %s: %s", rel, exc)

    def run_sync() -> None:
        try:
            with db.connect(settings.database_url) as conn, ingest_lock(conn):
                log.info("Sincronización: %s", ingest(conn, docs_dir, embedder).summary())
        except Exception as exc:  # noqa: BLE001 - worker job: nobody else would see the error
            log.error("La sincronización falló: %s", exc)

    @router.get("/documents")
    def list_documents(principal=Depends(require_admin), conn=Depends(get_conn)):
        return admin.list_documents(conn)

    @router.get("/collections")
    def list_collections(principal=Depends(require_admin), conn=Depends(get_conn)):
        return admin.list_collections(conn, docs_dir)

    @router.post("/documents", status_code=202)
    def upload_document(
        file: Annotated[UploadFile, File()],
        collection: Annotated[str, Form()],
        replace: Annotated[bool, Form()] = False,
        principal: auth.Principal = Depends(require_principal),
        conn=Depends(get_conn),
    ):
        try:
            name = admin.validate_collection(collection)
        except admin.InvalidUpload as exc:
            raise HTTPException(400, str(exc)) from None
        require_can_manage(principal, name)
        if not docs_dir.is_dir():
            raise HTTPException(503, "La carpeta de documentos no está disponible")
        try:
            rel = admin.save_upload(
                docs_dir,
                name,
                file.filename or "",
                file.file,
                settings.max_upload_mb * 1024 * 1024,
                replace=replace,
            )
        except admin.UploadTooLarge as exc:
            raise HTTPException(413, str(exc)) from None
        except admin.UploadExists as exc:
            raise HTTPException(409, str(exc)) from None
        except admin.InvalidUpload as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError as exc:
            log.error("No se pudo guardar la subida en %s: %s", docs_dir, exc)
            raise HTTPException(
                500, "No se pudo escribir en la carpeta de documentos (¿permisos?)"
            ) from None
        mark_processing(conn, rel)
        worker.submit(process_upload, rel)
        audit(principal, "upload", rel)
        return {"path": str(rel), "status": "processing"}

    @router.delete("/documents/{path:path}", status_code=204)
    def delete_document(
        path: str, principal: auth.Principal = Depends(require_principal), conn=Depends(get_conn)
    ):
        require_can_manage(principal, collection_for(PurePosixPath(path)))
        try:
            admin.delete_document(conn, docs_dir, path)
        except LookupError:
            raise HTTPException(404, "Documento no encontrado") from None
        except admin.IndexBusy:
            raise HTTPException(409, BUSY_MESSAGE) from None
        except admin.InvalidUpload as exc:
            raise HTTPException(400, str(exc)) from None
        audit(principal, "delete", path)
        return Response(status_code=204)

    @router.post("/sync", status_code=202)
    def sync(principal=Depends(require_admin), conn=Depends(get_conn)):
        if ingest_busy(conn):
            raise HTTPException(409, BUSY_MESSAGE)
        worker.submit(run_sync)
        audit(principal, "sync", docs_dir)
        return {"status": "started"}

    @router.get("/users")
    def list_users(principal=Depends(require_admin), conn=Depends(get_conn)):
        return auth.list_users(conn)

    @router.post("/users", status_code=201)
    def create_user(body: NewUser, principal=Depends(require_admin), conn=Depends(get_conn)):
        try:
            auth.create_user(
                conn, body.username, body.password, body.collections, is_admin=body.is_admin
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        user = auth.get_user(conn, body.username)
        audit(principal, "user_create", user["username"])
        return user

    @router.patch("/users/{username}")
    def update_user(
        username: str, body: UserUpdate, principal=Depends(require_admin), conn=Depends(get_conn)
    ):
        user = auth.get_user(conn, username)
        if user is None:
            raise HTTPException(404, "Usuario no encontrado")
        if body.is_admin is False and user["username"] == principal.name:
            raise HTTPException(409, "No puedes quitarte el rol de administrador a ti mismo")
        if body.collections is not None:
            auth.set_collections(conn, username, body.collections)
        if body.is_admin is not None:
            auth.set_admin(conn, username, body.is_admin)
        audit(principal, "user_update", user["username"])
        return auth.get_user(conn, username)

    @router.post("/users/{username}/password", status_code=204)
    def reset_password(
        username: str, body: NewPassword, principal=Depends(require_admin), conn=Depends(get_conn)
    ):
        if not auth.set_password(conn, username, body.password):
            raise HTTPException(404, "Usuario no encontrado")
        audit(principal, "user_password", username.strip().lower())
        return Response(status_code=204)

    @router.delete("/users/{username}", status_code=204)
    def delete_user(username: str, principal=Depends(require_admin), conn=Depends(get_conn)):
        if username.strip().lower() == principal.name:
            raise HTTPException(409, "No puedes borrar tu propio usuario")
        if not auth.delete_user(conn, username):
            raise HTTPException(404, "Usuario no encontrado")
        audit(principal, "user_delete", username.strip().lower())
        return Response(status_code=204)

    return router
