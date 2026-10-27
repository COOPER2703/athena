"""Jetons d'accès : cycle de vie, sur une vraie base SQLite.

Ces tests parlent au port ``AuthService`` (création, liste, validation,
révocation, dernière utilisation) et n'observent que des effets externes : le
secret rendu une fois, la ligne telle que la liste la montre, la validation
acceptée ou refusée. Aucune assertion ne porte sur le schéma ni sur une méthode
privée — sauf la nature de ce qui est stocké, qui est précisément l'exigence de
la spec (« la base ne stocke que l'empreinte et le préfixe »).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from conftest import RecordingTraceSink
from server.adapters.auth import SqliteAuthStore
from server.core.ports import AuthError

PASSWORD = "correct horse battery staple"


class FakeClock:
    """Horloge injectée : la résolution de dernière utilisation est réelle."""

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
    **options: object,
) -> SqliteAuthStore:
    return SqliteAuthStore(
        db_path=str(tmp_path / "auth.db"),
        trace_sink=RecordingTraceSink(),
        now=clock or FakeClock(),
        **options,
    )


async def _installed_store(
    tmp_path: Path, *, clock: FakeClock | None = None
) -> SqliteAuthStore:
    """Store installé : les Jetons n'existent que dans une instance configurée."""
    store = _store(tmp_path, clock=clock)
    code = store.announce_installation_code()
    assert code is not None
    await store.install(code, "admin", PASSWORD)
    return store


async def _token_error(
    store: SqliteAuthStore, name: str, kind: str, source: str | None = None
) -> AuthError:
    with pytest.raises(AuthError) as excinfo:
        await store.create_token(name=name, kind=kind, source=source)
    return excinfo.value


def test_a_created_token_is_returned_once_and_resolved_by_its_kind(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        store = await _installed_store(tmp_path, clock=clock)

        issued = await store.create_token(name="Portable", kind="client")
        assert issued.secret.startswith("ath_")
        assert len(issued.secret) > len("ath_") + 32
        assert issued.token.name == "Portable"
        assert issued.token.kind == "client"
        assert issued.token.source is None
        assert issued.token.prefix == issued.secret[: len(issued.token.prefix)]
        assert issued.token.created_at == pytest.approx(clock.value)
        assert issued.token.last_used_at is None
        assert issued.token.revoked_at is None

        # Le secret est le credential : la validation le résout en Jeton.
        resolved = await store.authenticate_token(issued.secret, "client")
        assert resolved is not None
        assert resolved.id == issued.token.id
        assert resolved.name == "Portable"

        # Un Jeton ne sert pas sur une surface pour laquelle il n'est pas né.
        assert (await store.authenticate_token(issued.secret, "source")) is None

        # Un secret inconnu est refusé, sans exception.
        assert (await store.authenticate_token("ath_inconnu", "client")) is None
        assert (await store.authenticate_token("", "client")) is None

        # Deux Jetons ne partagent jamais leur secret : 256 bits aléatoires.
        second = await store.create_token(name="Autre", kind="client")
        assert second.secret.startswith("ath_")
        assert second.secret != issued.secret
        assert len(second.secret) == len(issued.secret)

    asyncio.run(scenario())


def test_the_list_shows_the_token_without_ever_revealing_the_secret(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = await _installed_store(tmp_path)
        issued = await store.create_token(name="Portable", kind="client")

        [listed] = await store.list_tokens()

        assert listed == issued.token
        assert listed.prefix.startswith("ath_")
        assert issued.secret not in repr(listed)
        assert hashlib.sha256(issued.secret.encode()).hexdigest() not in repr(listed)

    asyncio.run(scenario())


def test_a_source_token_requires_a_source_and_a_client_token_forbids_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = await _installed_store(tmp_path)

        assert (await _token_error(store, "Esp32", "source")).code == "invalid_source"
        assert (
            await _token_error(store, "Esp32", "source", "   ")
        ).code == "invalid_source"
        assert (
            await _token_error(store, "Portable", "client", "matrix")
        ).code == "invalid_source"

        # Une nature inconnue et un nom vide sont refusés avant toute écriture.
        assert (await _token_error(store, "X", "admin")).code == "invalid_kind"
        assert (
            await _token_error(store, "   ", "client")
        ).code == "invalid_token_name"

        assert await store.list_tokens() == []

        # Les deux natures valides sont acceptées, avec leur source pour source.
        client = await store.create_token(name="  Portable  ", kind="client")
        source = await store.create_token(name="Esp32", kind="source", source=" matrix ")
        assert client.token.name == "Portable"
        assert client.token.source is None
        assert source.token.kind == "source"
        assert source.token.source == "matrix"

    asyncio.run(scenario())


def test_revoking_a_token_takes_effect_immediately_and_stays_visible(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        store = await _installed_store(tmp_path, clock=clock)
        kept = await store.create_token(name="Portable", kind="client")
        doomed = await store.create_token(name="Esp32", kind="source", source="esp")

        clock.advance(60)
        revoked = await store.revoke_token(doomed.token.id)
        assert revoked is not None
        assert revoked.id == doomed.token.id
        assert revoked.revoked_at == pytest.approx(clock.value)

        # Le credential ne vaut plus rien, dès la validation suivante.
        assert (await store.authenticate_token(doomed.secret, "source")) is None
        # Les autres Jetons ne sont pas touchés.
        assert (await store.authenticate_token(kept.secret, "client")) is not None

        # La révocation est visible dans la liste, à sa date d'origine.
        listed = {token.id: token for token in await store.list_tokens()}
        assert listed[doomed.token.id].revoked_at == pytest.approx(clock.value)
        assert listed[kept.token.id].revoked_at is None

        # Révoquer deux fois ne réécrit pas la date, et un identifiant inconnu
        # n'invente rien.
        clock.advance(60)
        again = await store.revoke_token(doomed.token.id)
        assert again is not None
        assert again.revoked_at == pytest.approx(clock.value - 60)
        assert (await store.revoke_token("inconnu")) is None

    asyncio.run(scenario())


def test_the_last_use_is_recorded_at_most_once_per_minute(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        store = await _installed_store(tmp_path, clock=clock)
        issued = await store.create_token(name="Portable", kind="client")

        [listed] = await store.list_tokens()
        assert listed.last_used_at is None

        await store.mark_token_used(issued.token.id)
        [used] = await store.list_tokens()
        assert used.last_used_at == pytest.approx(clock.value)

        # Une seconde plus tard, l'usage n'est pas réécrit : pas d'écriture par
        # trame pendant une Session.
        clock.advance(1)
        await store.mark_token_used(issued.token.id)
        [still] = await store.list_tokens()
        assert still.last_used_at == pytest.approx(clock.value - 1)

        # Passé la minute, la date suit à nouveau.
        clock.advance(60)
        await store.mark_token_used(issued.token.id)
        [refreshed] = await store.list_tokens()
        assert refreshed.last_used_at == pytest.approx(clock.value)

    asyncio.run(scenario())


def test_a_revoked_token_is_never_marked_used(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        store = await _installed_store(tmp_path, clock=clock)
        issued = await store.create_token(name="Portable", kind="client")
        await store.revoke_token(issued.token.id)

        clock.advance(120)
        await store.mark_token_used(issued.token.id)

        [listed] = await store.list_tokens()
        assert listed.last_used_at is None
        assert listed.revoked_at == pytest.approx(clock.value - 120)

    asyncio.run(scenario())


def test_the_database_stores_only_the_digest_and_the_readable_prefix(
    tmp_path: Path,
) -> None:
    captured: dict[str, str] = {}

    async def scenario() -> None:
        store = await _installed_store(tmp_path)
        issued = await store.create_token(name="Portable", kind="client")
        [listed] = await store.list_tokens()
        assert listed.prefix == issued.secret[: len(listed.prefix)]
        captured["secret"] = issued.secret
        captured["prefix"] = listed.prefix

    asyncio.run(scenario())

    secret = captured["secret"]
    stored = (tmp_path / "auth.db").read_bytes()
    assert secret.encode() not in stored
    assert hashlib.sha256(secret.encode()).hexdigest().encode() in stored
    assert captured["prefix"].encode() in stored


