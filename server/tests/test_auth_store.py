"""Comportement du module d'authentification, sur une vraie base SQLite.

Ces tests parlent au port ``AuthService`` (installation, connexion, tickets) et
observent des effets externes : le Compte créé, le Ticket accepté ou refusé, le
Code d'installation écrit dans le flux de traces. Aucune assertion ne porte sur
le schéma SQL ni sur une méthode privée — sauf la nature du hachage stocké, qui
est précisément l'exigence de la spec (« Argon2id, jamais le mot de passe »).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conftest import RecordingTraceSink
from server.adapters.auth import SqliteAuthStore
from server.core.ports import AuthError

PASSWORD = "correct horse battery staple"


class FakeClock:
    """Horloge injectée : les seuils d'expiration et de blocage sont réels."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _store(
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
    sink: RecordingTraceSink | None = None,
    **options: object,
) -> SqliteAuthStore:
    return SqliteAuthStore(
        db_path=str(tmp_path / "auth.db"),
        trace_sink=sink,
        now=clock or FakeClock(),
        **options,
    )


def _announced_code(store: SqliteAuthStore, sink: RecordingTraceSink) -> str:
    assert store.announce_installation_code() is not None
    [entry] = sink.of_kind("InstallationCode")
    return str(entry.payload["code"])


def test_install_accepts_only_the_code_announced_at_startup(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path, sink=sink)
        code = _announced_code(store, sink)
        assert store.setup_required is True

        with pytest.raises(AuthError) as excinfo:
            await store.install("not-the-code", "admin", PASSWORD)
        assert excinfo.value.code == "invalid_code"
        assert store.setup_required is True

        ticket = await store.install(code, "admin", PASSWORD)
        assert ticket.account.username == "admin"
        assert store.setup_required is False
        assert (await store.authenticate_ticket(ticket.secret)) is not None

    asyncio.run(scenario())


def test_no_code_is_announced_once_an_account_exists(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path, sink=sink)
        await store.install(_announced_code(store, sink), "admin", PASSWORD)

        assert store.announce_installation_code() is None
        assert [entry.kind for entry in sink.entries] == ["InstallationCode"]

        # Un second démarrage sur la même base ne réémet rien non plus.
        restarted = _store(tmp_path, sink=sink)
        assert restarted.announce_installation_code() is None
        assert len(sink.of_kind("InstallationCode")) == 1

    asyncio.run(scenario())


def test_a_second_install_is_refused_even_with_the_right_code(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path, sink=sink)
        code = _announced_code(store, sink)
        await store.install(code, "admin", PASSWORD)

        with pytest.raises(AuthError) as excinfo:
            await store.install(code, "intruder", "another long password")

        assert excinfo.value.code == "already_installed"

    asyncio.run(scenario())


def test_the_password_is_stored_as_an_argon2id_hash(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path, sink=sink)
        await store.install(_announced_code(store, sink), "admin", PASSWORD)

    asyncio.run(scenario())

    stored = (tmp_path / "auth.db").read_bytes()
    assert PASSWORD.encode() not in stored
    assert b"$argon2id$" in stored


def test_the_server_refuses_a_password_below_the_minimum(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path, sink=sink, password_min_length=12)
        code = _announced_code(store, sink)

        with pytest.raises(AuthError) as excinfo:
            await store.install(code, "admin", "short")

        assert excinfo.value.code == "weak_password"
        assert store.setup_required is True

        ticket = await store.install(code, "admin", PASSWORD)
        assert ticket.account.username == "admin"

    asyncio.run(scenario())


def test_login_refuses_unknown_user_and_wrong_password_alike(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path, sink=sink)
        await store.install(_announced_code(store, sink), "admin", PASSWORD)

        wrong_password = await _login_error(store, "admin", "not the password")
        unknown_user = await _login_error(store, "somebody", PASSWORD)

        assert wrong_password.code == "invalid_credentials"
        assert unknown_user.code == "invalid_credentials"

        ticket = await store.login("admin", PASSWORD)
        session = await store.authenticate_ticket(ticket.secret)
        assert session is not None
        assert session.account.username == "admin"

    asyncio.run(scenario())


async def _login_error(
    store: SqliteAuthStore, username: str, password: str
) -> AuthError:
    with pytest.raises(AuthError) as excinfo:
        await store.login(username, password)
    return excinfo.value


def test_five_failures_lock_the_login_for_fifteen_minutes_then_lift(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        sink = RecordingTraceSink()
        store = _store(tmp_path, clock=clock, sink=sink)
        await store.install(_announced_code(store, sink), "admin", PASSWORD)

        for _ in range(4):
            assert (await _login_error(store, "admin", "wrong")).code == (
                "invalid_credentials"
            )

        locked = await _login_error(store, "admin", "wrong")
        assert locked.code == "too_many_attempts"
        assert locked.retry_after == pytest.approx(15 * 60)

        # Même le bon mot de passe est refusé pendant le blocage.
        still_locked = await _login_error(store, "admin", PASSWORD)
        assert still_locked.code == "too_many_attempts"
        assert still_locked.retry_after is not None
        assert 0 < still_locked.retry_after <= 15 * 60

        clock.advance(15 * 60)

        # Le blocage se lève seul, sans redémarrage.
        ticket = await store.login("admin", PASSWORD)
        assert ticket.account.username == "admin"

        # Un succès remet le compteur à zéro : quatre échecs restent des 401.
        for _ in range(4):
            assert (await _login_error(store, "admin", "wrong")).code == (
                "invalid_credentials"
            )

    asyncio.run(scenario())


def test_a_ticket_expires_after_thirty_days_and_slides_while_used(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        sink = RecordingTraceSink()
        store = _store(tmp_path, clock=clock, sink=sink)
        ticket = await store.install(_announced_code(store, sink), "admin", PASSWORD)

        session = await store.authenticate_ticket(ticket.secret)
        assert session is not None
        assert session.refreshed is False

        # Moins d'une heure plus tard : valide, sans écriture ni nouveau cookie.
        clock.advance(30 * 60)
        session = await store.authenticate_ticket(ticket.secret)
        assert session is not None
        assert session.refreshed is False

        # Plus d'une heure : l'expiration glisse.
        clock.advance(31 * 60)
        session = await store.authenticate_ticket(ticket.secret)
        assert session is not None
        assert session.refreshed is True
        assert session.expires_at == pytest.approx(clock.value + 30 * 86400)

        # Un ticket laissé de côté 30 jours expire.
        clock.advance(30 * 86400)
        assert (await store.authenticate_ticket(ticket.secret)) is None

    asyncio.run(scenario())


def test_a_revoked_ticket_is_refused_immediately(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path, sink=sink)
        await store.install(_announced_code(store, sink), "admin", PASSWORD)
        ticket = await store.login("admin", PASSWORD)

        await store.revoke_ticket(ticket.secret)

        assert (await store.authenticate_ticket(ticket.secret)) is None

    asyncio.run(scenario())


def test_setup_attempts_are_capped_globally(tmp_path: Path) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path, sink=sink, setup_max_attempts=2)
        code = _announced_code(store, sink)

        assert (await _install_error(store, "nope", "admin", PASSWORD)).code == (
            "invalid_code"
        )
        limited = await _install_error(store, "nope", "admin", PASSWORD)
        assert limited.code == "too_many_attempts"
        assert limited.retry_after is not None

        # Même le bon code attend la fin du blocage.
        blocked = await _install_error(store, code, "admin", PASSWORD)
        assert blocked.code == "too_many_attempts"

    asyncio.run(scenario())


async def _install_error(
    store: SqliteAuthStore, code: str, username: str, password: str
) -> AuthError:
    with pytest.raises(AuthError) as excinfo:
        await store.install(code, username, password)
    return excinfo.value
