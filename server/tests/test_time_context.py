"""Contexte temporel injecté dans les prompts (ticket #192, ADR-0028).

Deux coutures : le rendu pur (``format_now_context``) pour les passages
d'heure d'été, et ``build_app`` pour prouver que le fuseau du Réglage arrive
jusqu'à l'instruction d'une Session réelle. Aucun test n'attend de temps réel :
l'horloge murale est injectée.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from server.app.root import format_now_context
from server_harness import running_server

PARIS = "Europe/Paris"


def test_the_context_renders_the_french_date_and_local_time_to_the_second() -> None:
    moment = datetime(2026, 11, 27, 14, 32, 5, tzinfo=timezone.utc)
    rendered = format_now_context(moment, "UTC")

    assert rendered == (
        "[CONTEXTE TEMPOREL]\n"
        "Nous sommes le vendredi 27 novembre 2026, il est 14:32:05 (UTC)."
    )


def test_spring_forward_renders_the_shifted_local_time() -> None:
    # 2026-03-29 : à 01:00 UTC, Paris passe de 02:00 CET à 03:00 CEST.
    moment = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)

    rendered = format_now_context(moment, PARIS)

    assert "il est 03:30:00 (Europe/Paris)." in rendered


def test_fall_back_renders_the_winter_local_time() -> None:
    # 2026-10-25 : à 01:00 UTC, Paris repasse de 03:00 CEST à 02:00 CET.
    moment = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)

    rendered = format_now_context(moment, PARIS)

    assert "il est 02:30:00 (Europe/Paris)." in rendered


def test_build_app_renders_the_injected_wall_clock(tmp_path: Path) -> None:
    """La couture ``wall_clock`` fige la section sans attendre le temps réel."""
    spring_forward = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)

    async def scenario() -> None:
        async with running_server(
            tmp_path,
            env={"ATHENA_TIMEZONE": PARIS},
            wall_clock=lambda: spring_forward,
        ) as server:
            await server.install()
            async with server.client() as client:
                await client.start_session()
                instruction = server.model.instruction()

        assert (
            "[CONTEXTE TEMPOREL]\n"
            "Nous sommes le dimanche 29 mars 2026, "
            "il est 03:30:00 (Europe/Paris)."
        ) in instruction

    asyncio.run(scenario())


def test_build_app_injects_the_instance_timezone_into_a_session_instruction(
    tmp_path: Path,
) -> None:
    """Le fuseau du Réglage arrive jusqu'à la vraie instruction Live."""

    async def scenario() -> None:
        async with running_server(
            tmp_path, env={"ATHENA_TIMEZONE": "Asia/Tokyo"}
        ) as server:
            await server.install()
            async with server.client() as client:
                await client.start_session()
                instruction = server.model.instruction()

        assert "[CONTEXTE TEMPOREL]" in instruction
        assert "Nous sommes le " in instruction
        assert "(Asia/Tokyo)." in instruction

    asyncio.run(scenario())
