from types import SimpleNamespace

import pytest

from cka import auth


def fake_request(headers=None, session=None):
    return SimpleNamespace(headers=headers or {}, session=session or {})


def session_for(conn, username):
    session = {}
    auth.start_session(conn, session, username)
    return fake_request(session=session)


def test_password_hash_roundtrip():
    stored = auth.hash_password("secreto-123")
    assert stored.startswith("scrypt$")
    assert "secreto-123" not in stored
    assert auth.verify_password("secreto-123", stored)
    assert not auth.verify_password("otra", stored)
    assert auth.hash_password("secreto-123") != stored  # random salt


def test_verify_password_rejects_malformed_hash():
    assert not auth.verify_password("x", "garbage")
    assert not auth.verify_password("x", "md5$aa$bb")


def test_normalize_collections_adds_general_and_lowercases():
    assert auth.normalize_collections([" RRHH", "rrhh", "", "Ventas"]) == (
        "general",
        "rrhh",
        "ventas",
    )


def test_throttle_locks_after_max_failures_and_expires():
    now = [0.0]
    throttle = auth.LoginThrottle(max_failures=3, lockout_seconds=60, clock=lambda: now[0])
    for _ in range(3):
        throttle.record_failure("ana")
    assert throttle.is_locked("ana")
    now[0] = 61.0
    assert not throttle.is_locked("ana")


def test_throttle_success_resets_failures():
    throttle = auth.LoginThrottle(max_failures=3)
    throttle.record_failure("ana")
    throttle.record_failure("ana")
    throttle.record_success("ana")
    throttle.record_failure("ana")
    assert not throttle.is_locked("ana")


def test_authenticate(conn):
    auth.create_user(conn, " Ana ", "contraseña-ana", ["RRHH"])
    throttle = auth.LoginThrottle()
    assert auth.authenticate(conn, "ANA", "contraseña-ana", throttle) == auth.Principal(
        "ana", ("general", "rrhh")
    )
    assert auth.authenticate(conn, "ana", "mala", throttle) is None
    assert auth.authenticate(conn, "nadie", "x", throttle) is None


def test_locked_account_rejects_even_the_correct_password(conn):
    auth.create_user(conn, "ana", "contraseña-ana", [])
    throttle = auth.LoginThrottle(max_failures=2)
    auth.authenticate(conn, "ana", "mala", throttle)
    auth.authenticate(conn, "ana", "mala", throttle)
    with pytest.raises(auth.AccountLocked):
        auth.authenticate(conn, "ana", "contraseña-ana", throttle)


def test_create_user_rejects_duplicates(conn):
    auth.create_user(conn, "ana", "contraseña-ana", [])
    with pytest.raises(ValueError, match="ya existe"):
        auth.create_user(conn, "ANA", "otra-contraseña", [])


def test_user_management(conn):
    auth.create_user(conn, "ana", "contraseña-ana", ["rrhh"])
    throttle = auth.LoginThrottle()
    assert auth.set_password(conn, "ana", "nueva-contraseña")
    assert auth.authenticate(conn, "ana", "nueva-contraseña", throttle) is not None
    assert auth.set_collections(conn, "ana", ["Finanzas"])
    assert auth.authenticate(conn, "ana", "nueva-contraseña", throttle).collections == (
        "finanzas",
        "general",
    )
    assert auth.delete_user(conn, "ana")
    assert not auth.delete_user(conn, "ana")
    assert not auth.set_password(conn, "nadie", "x")
    assert not auth.set_collections(conn, "nadie", [])


def test_api_keys(conn):
    raw = auth.create_api_key(conn, "intranet", ["Finanzas"])
    assert raw.startswith("cka_")
    stored = conn.execute("SELECT key_hash FROM api_keys").fetchone()[0]
    assert raw not in stored
    assert auth.principal_from_api_key(conn, raw) == auth.Principal(
        "api:intranet", ("finanzas", "general")
    )
    assert auth.principal_from_api_key(conn, "cka_wrong") is None
    with pytest.raises(ValueError):
        auth.create_api_key(conn, "intranet", [])
    assert auth.revoke_api_key(conn, "intranet")
    assert auth.principal_from_api_key(conn, raw) is None
    assert not auth.revoke_api_key(conn, "intranet")


def test_get_principal_from_bearer_or_session(conn):
    auth.create_user(conn, "ana", "contraseña-ana", ["rrhh"])
    raw = auth.create_api_key(conn, "bot", ["ventas"])
    by_key = auth.get_principal(fake_request(headers={"authorization": f"Bearer {raw}"}), conn)
    assert by_key.name == "api:bot"
    by_session = auth.get_principal(session_for(conn, "ana"), conn)
    assert by_session.collections == ("general", "rrhh")
    assert auth.get_principal(fake_request(), conn) is None
    bad_key_with_session = fake_request(
        headers={"authorization": "Bearer nope"}, session={"user": "ana"}
    )
    assert auth.get_principal(bad_key_with_session, conn) is None


def test_session_reflects_permission_changes_immediately(conn):
    auth.create_user(conn, "ana", "contraseña-ana", ["rrhh"])
    request = session_for(conn, "ana")
    auth.set_collections(conn, "ana", ["finanzas"])
    assert auth.get_principal(request, conn).collections == ("finanzas", "general")
    auth.delete_user(conn, "ana")
    assert auth.get_principal(request, conn) is None


def test_session_without_epoch_is_rejected(conn):
    auth.create_user(conn, "ana", "contraseña-ana", [])
    assert auth.get_principal(fake_request(session={"user": "ana"}), conn) is None


def test_password_change_invalidates_open_sessions(conn):
    auth.create_user(conn, "ana", "contraseña-ana", [])
    request = session_for(conn, "ana")
    assert auth.get_principal(request, conn) is not None
    auth.set_password(conn, "ana", "nueva-contraseña")
    assert auth.get_principal(request, conn) is None


def test_recreated_user_does_not_inherit_old_sessions(conn):
    auth.create_user(conn, "ana", "contraseña-ana", ["rrhh"])
    request = session_for(conn, "ana")
    auth.delete_user(conn, "ana")
    auth.create_user(conn, "ana", "otra-contraseña", ["finanzas"])
    assert auth.get_principal(request, conn) is None


def test_old_failures_do_not_count_towards_lockout():
    now = [0.0]
    throttle = auth.LoginThrottle(max_failures=3, lockout_seconds=60, clock=lambda: now[0])
    throttle.record_failure("ana")
    throttle.record_failure("ana")
    now[0] = 1000.0
    throttle.record_failure("ana")
    assert not throttle.is_locked("ana")


def test_throttle_forgets_stale_usernames():
    now = [0.0]
    throttle = auth.LoginThrottle(lockout_seconds=60, clock=lambda: now[0], max_tracked=10)
    for i in range(10):
        throttle.record_failure(f"random-{i}")
    now[0] = 1000.0
    throttle.record_failure("ana")
    assert throttle.tracked() == 1


def test_revoked_api_key_name_can_be_reused(conn):
    old = auth.create_api_key(conn, "intranet", [])
    auth.revoke_api_key(conn, "intranet")
    new = auth.create_api_key(conn, "intranet", ["ventas"])
    assert auth.principal_from_api_key(conn, old) is None
    assert auth.principal_from_api_key(conn, new).collections == ("general", "ventas")
