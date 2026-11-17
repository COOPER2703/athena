from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any, Callable, Mapping

import aiohttp
from aiohttp import web

from client.config import AppConfig, PageConfig, WakeWordConfig
from client.daemon import ClientDaemon
from client.settings import SettingsStore
from client.wake_word import WakeWordUnavailableError


BAD_TOKEN = "ath_bad_secret"


class BlockingAudio:
    """Audio factice : les flux restent ouverts jusqu'à la fermeture."""

    def __init__(self) -> None:
        self.started_input = False
        self.started_output = False
        self.closed = False
        self._gate = asyncio.Event()

    async def start_input(self) -> None:
        self.started_input = True

    async def start_output(self) -> None:
        self.started_output = True

    async def read_chunk(self) -> bytes:
        await self._gate.wait()
        return b""

    async def write_chunk(self, data: bytes) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True
        self._gate.set()


class FailingAudio(BlockingAudio):
    async def start_input(self) -> None:
        raise OSError("no microphone")


class UnavailableDetector:
    def process(self, chunk: bytes) -> None:
        return None

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        wake_word=WakeWordConfig(enabled=False),
        page=PageConfig(
            host="127.0.0.1", port=0, config_path=tmp_path / "client.json"
        ),
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TokenRefusingServer:
    """Serveur qui refuse tout handshake par 401, en comptant les tentatives."""

    def __init__(self) -> None:
        self.attempts = 0
        self.authorizations: list[str] = []
        self._runner: web.AppRunner | None = None

    async def start(self) -> int:
        async def refuse(request: web.Request) -> web.Response:
            self.attempts += 1
            self.authorizations.append(request.headers.get("Authorization", ""))
            return web.Response(status=401, text="unauthorized")

        application = web.Application()
        application.router.add_get("/", refuse)
        self._runner = web.AppRunner(application)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        sockets = site._server.sockets  # type: ignore[union-attr]
        return int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None


async def get_state(
    session: aiohttp.ClientSession, base: str
) -> Mapping[str, Any]:
    async with session.get(f"{base}/api/state") as response:
        assert response.status == 200
        return await response.json()


async def wait_for_state(
    base: str,
    predicate: Callable[[Mapping[str, Any]], bool],
    timeout: float = 5.0,
) -> Mapping[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with aiohttp.ClientSession() as session:
        while True:
            state = await get_state(session, base)
            if predicate(state):
                return state
            if loop.time() > deadline:
                raise AssertionError(f"état attendu non atteint : {state}")
            await asyncio.sleep(0.02)


async def start_daemon(
    cfg: AppConfig,
    *,
    environ: Mapping[str, str] | None = None,
    audio: BlockingAudio | None = None,
    detector_factory: Callable[[], Any] | None = None,
) -> ClientDaemon:
    daemon = ClientDaemon(
        cfg,
        SettingsStore(cfg.page.config_path),
        environ={} if environ is None else environ,
        audio_factory=lambda: audio or BlockingAudio(),
        detector_factory=detector_factory,
    )
    await daemon.start()
    return daemon


def base_url(daemon: ClientDaemon) -> str:
    return f"http://127.0.0.1:{daemon.page.port}"


def test_the_page_is_served_before_any_coupling(tmp_path: Path) -> None:
    async def scenario() -> None:
        daemon = await start_daemon(app_config(tmp_path))
        try:
            async with aiohttp.ClientSession() as session:
                state = await get_state(session, base_url(daemon))
                async with session.get(f"{base_url(daemon)}/") as response:
                    assert response.status == 200
                    body = await response.text()
            assert state["token_configured"] is False
            assert state["token_source"] == "none"
            assert state["connection"] == "unconfigured"
            assert "Jeton d'accès" in body
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_an_unreachable_server_does_not_hide_the_page(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SettingsStore(app_config(tmp_path).page.config_path)
        store.save(
            server_url=f"ws://127.0.0.1:{free_port()}", access_token=BAD_TOKEN
        )
        daemon = await start_daemon(app_config(tmp_path))
        try:
            state = await wait_for_state(
                base_url(daemon), lambda s: s["connection"] == "error"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url(daemon)}/") as response:
                    assert response.status == 200
            assert "injoignable" in state["connection_message"].lower()
            assert state["token_configured"] is True
            assert BAD_TOKEN not in str(state)
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_an_invalid_token_is_explained_without_stopping_the_daemon(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        server = TokenRefusingServer()
        port = await server.start()
        cfg = app_config(tmp_path)
        SettingsStore(cfg.page.config_path).save(
            server_url=f"ws://127.0.0.1:{port}", access_token=BAD_TOKEN
        )
        daemon = await start_daemon(cfg)
        try:
            state = await wait_for_state(
                base_url(daemon), lambda s: s["connection"] == "refused"
            )
            assert "Jeton" in state["connection_message"]
            assert "refus" in state["connection_message"].lower()
            assert BAD_TOKEN not in str(state)
            assert server.authorizations[0] == f"Bearer {BAD_TOKEN}"

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url(daemon)}/api/reconnect"
                ) as response:
                    assert response.status == 200
            await wait_for_state(
                base_url(daemon), lambda s: s["connection"] == "refused"
            )
            assert server.attempts >= 2
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url(daemon)}/") as response:
                    assert response.status == 200
        finally:
            await daemon.stop()
            await server.stop()

    asyncio.run(scenario())


def test_a_saved_coupling_survives_a_daemon_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = TokenRefusingServer()
        port = await server.start()
        cfg = app_config(tmp_path)
        daemon = await start_daemon(cfg)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url(daemon)}/api/coupling",
                    json={
                        "server_url": f"ws://127.0.0.1:{port}",
                        "access_token": BAD_TOKEN,
                    },
                ) as response:
                    assert response.status == 200
                    payload = await response.text()
            assert BAD_TOKEN not in payload
            await wait_for_state(
                base_url(daemon), lambda s: s["connection"] == "refused"
            )
        finally:
            await daemon.stop()

        assert SettingsStore(cfg.page.config_path).load().access_token == BAD_TOKEN

        second = await start_daemon(cfg)
        try:
            state = await wait_for_state(
                base_url(second), lambda s: s["connection"] == "refused"
            )
            assert state["token_configured"] is True
            assert state["token_source"] == "file"
            assert state["server_url_source"] == "file"
            assert state["server_url"] == f"ws://127.0.0.1:{port}"
            assert server.attempts >= 2
        finally:
            await second.stop()
            await server.stop()

    asyncio.run(scenario())


def test_the_environment_token_wins_over_the_saved_one(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = TokenRefusingServer()
        port = await server.start()
        cfg = app_config(tmp_path)
        SettingsStore(cfg.page.config_path).save(
            server_url=f"ws://127.0.0.1:{port}", access_token="ath_from_file"
        )
        daemon = await start_daemon(
            cfg, environ={"ATHENA_ACCESS_TOKEN": "ath_from_env"}
        )
        try:
            state = await wait_for_state(
                base_url(daemon), lambda s: s["connection"] == "refused"
            )
            assert state["token_source"] == "environment"
            assert server.authorizations[0] == "Bearer ath_from_env"
        finally:
            await daemon.stop()
            await server.stop()

    asyncio.run(scenario())


def test_the_page_reports_the_microphone_and_a_disabled_wake_word(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        audio = BlockingAudio()
        daemon = await start_daemon(app_config(tmp_path), audio=audio)

        async def coupled() -> None:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url(daemon)}/api/coupling",
                    json={
                        "server_url": f"ws://127.0.0.1:{free_port()}",
                        "access_token": BAD_TOKEN,
                    },
                ) as response:
                    assert response.status == 200

        try:
            await coupled()
            state = await wait_for_state(
                base_url(daemon), lambda s: s["mic"] == "ready"
            )
            assert state["wake_word"] == "disabled"
            assert audio.started_input is True
            assert audio.started_output is True
        finally:
            await daemon.stop()
        assert audio.closed is True

    asyncio.run(scenario())


def test_a_missing_wake_word_model_does_not_hide_the_page(
    tmp_path: Path,
) -> None:
    def unavailable() -> UnavailableDetector:
        raise WakeWordUnavailableError("Modèle introuvable : /tmp/absent.onnx")

    async def scenario() -> None:
        cfg = AppConfig(
            wake_word=WakeWordConfig(enabled=True),
            page=PageConfig(
                host="127.0.0.1", port=0, config_path=tmp_path / "client.json"
            ),
        )
        SettingsStore(cfg.page.config_path).save(
            server_url=f"ws://127.0.0.1:{free_port()}", access_token=BAD_TOKEN
        )
        daemon = await start_daemon(cfg, detector_factory=unavailable)
        try:
            state = await wait_for_state(
                base_url(daemon), lambda s: s["wake_word"] == "unavailable"
            )
            assert "/tmp/absent.onnx" in state["wake_word_message"]
            assert state["mic"] in {"ready", "unknown"}
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url(daemon)}/") as response:
                    assert response.status == 200
        finally:
            await daemon.stop()

    asyncio.run(scenario())


def test_a_missing_microphone_is_reported_without_stopping_the_daemon(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        cfg = app_config(tmp_path)
        SettingsStore(cfg.page.config_path).save(
            server_url=f"ws://127.0.0.1:{free_port()}", access_token=BAD_TOKEN
        )
        daemon = await start_daemon(cfg, audio=FailingAudio())
        try:
            state = await wait_for_state(
                base_url(daemon), lambda s: s["mic"] == "unavailable"
            )
            assert "microphone" in state["mic_message"].lower()
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url(daemon)}/") as response:
                    assert response.status == 200
        finally:
            await daemon.stop()

    asyncio.run(scenario())
