"""Admin command line: `cka ingest`, user and API key management."""

import argparse
import getpass
import logging
import sys
from pathlib import Path

from cka import auth, db
from cka.config import Settings
from cka.embeddings import make_embedder
from cka.ingest import ingest

MIN_PASSWORD_CHARS = 8


def _collections(value: str) -> list[str]:
    return value.split(",")


def _read_password() -> str:
    password = getpass.getpass("Contraseña: ")
    if len(password) < MIN_PASSWORD_CHARS:
        raise SystemExit(f"La contraseña debe tener al menos {MIN_PASSWORD_CHARS} caracteres")
    if getpass.getpass("Repite la contraseña: ") != password:
        raise SystemExit("Las contraseñas no coinciden")
    return password


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cka", description="Company Knowledge Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="Load new or changed documents from DOCS_DIR")
    p.add_argument(
        "--reindex",
        action="store_true",
        help="Rebuild the whole index (after changing EMBEDDING_MODEL)",
    )
    p = sub.add_parser("create-user", help="Create a user (asks for the password)")
    p.add_argument("username")
    p.add_argument(
        "--collections", type=_collections, default=[], help="Comma-separated, e.g. rrhh,ventas"
    )
    p = sub.add_parser("set-password", help="Change a user's password")
    p.add_argument("username")
    p = sub.add_parser("set-collections", help="Replace a user's collections")
    p.add_argument("username")
    p.add_argument("--collections", type=_collections, required=True)
    p = sub.add_parser("delete-user", help="Delete a user")
    p.add_argument("username")
    p = sub.add_parser("create-api-key", help="Create an API key for another system (shown once)")
    p.add_argument("name")
    p.add_argument("--collections", type=_collections, default=[])
    p = sub.add_parser("revoke-api-key", help="Revoke an API key")
    p.add_argument("name")
    return parser


def _ingest(settings: Settings, reindex: bool) -> int:
    # Checked before anything else: --reindex must never drop the index for a missing folder.
    if not Path(settings.docs_dir).is_dir():
        return _fail(f"DOCS_DIR no existe o no es una carpeta: {settings.docs_dir}")
    embedder = make_embedder(settings)
    if reindex:
        db.reset_index(settings.database_url)
    try:
        db.init_schema(settings.database_url, embedder.dim, embedder.model_name)
    except db.EmbeddingModelMismatch as exc:
        return _fail(str(exc))
    with db.connect(settings.database_url) as conn:
        try:
            report = ingest(conn, Path(settings.docs_dir), embedder)
        except FileNotFoundError as exc:
            return _fail(str(exc))
    print(report.summary())
    return 1 if report.errors else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings.from_env()
    if args.command == "ingest":
        return _ingest(settings, args.reindex)

    db.init_auth_schema(settings.database_url)
    with db.connect(settings.database_url) as conn:
        if args.command == "create-user":
            try:
                auth.create_user(conn, args.username, _read_password(), args.collections)
            except ValueError as exc:
                return _fail(str(exc))
            print(f"Usuario {args.username} creado")
        elif args.command == "set-password":
            if not auth.set_password(conn, args.username, _read_password()):
                return _fail(f"No existe el usuario {args.username!r}")
            print("Contraseña actualizada")
        elif args.command == "set-collections":
            if not auth.set_collections(conn, args.username, args.collections):
                return _fail(f"No existe el usuario {args.username!r}")
            collections = ", ".join(auth.normalize_collections(args.collections))
            print(f"Colecciones de {args.username}: {collections}")
        elif args.command == "delete-user":
            if not auth.delete_user(conn, args.username):
                return _fail(f"No existe el usuario {args.username!r}")
            print(f"Usuario {args.username} eliminado")
        elif args.command == "create-api-key":
            try:
                key = auth.create_api_key(conn, args.name, args.collections)
            except ValueError as exc:
                return _fail(str(exc))
            print("Guarda esta clave ahora, no se volverá a mostrar:")
            print(key)
        elif args.command == "revoke-api-key":
            if not auth.revoke_api_key(conn, args.name):
                return _fail(f"No hay una API key activa llamada {args.name!r}")
            print(f"API key {args.name} revocada")
    return 0
