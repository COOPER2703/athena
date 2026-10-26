from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import websockets

from conftest import FakeJudge, RecordingTraceSink, _url, register, wait_until
from protocol import Audio, SessionStart, SessionStarted, decode, encode
from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.plugins import PluginLoader
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App, server_tool_names
from server.app.secrets import EnvSecretResolver
from server.core.plugins import PluginRegistry


class _FakeSession:
    async def send_realtime_input(self, **kwargs: Any) -> None:
        pass

    async def receive(self) -> Any:
        await asyncio.Event().wait()
        yield None  # pragma: no cover - never reached


class _FakeContext:
    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeLiveConnect:
    def __call__(self, model: str, config: Any) -> _FakeContext:
        return _FakeContext()


class _TalkingSession:
    """Fake Live session: one audio turn makes the Session visible, then blocks."""

    def __init__(self, audio: bytes) -> None:
        self._audio = audio

    async def send_realtime_input(self, **kwargs: Any) -> None:
        pass

    async def receive(self) -> Any:
        yield SimpleNamespace(data=self._audio)
        await asyncio.Event().wait()
        yield None  # pragma: no cover - never reached


class _TalkingContext:
    def __init__(self, audio: bytes) -> None:
        self._audio = audio

    async def __aenter__(self) -> _TalkingSession:
        return _TalkingSession(self._audio)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _TalkingLiveConnect:
    """Fait parler chaque Connexion Live ouverte, pour rendre les Sessions visibles."""

    def __init__(self, audio: bytes = b"reply") -> None:
        self._audio = audio

    def __call__(self, model: str, config: Any) -> _TalkingContext:
        return _TalkingContext(self._audio)


def _session_visible(sink: RecordingTraceSink) -> bool:
    return any(
        entry.source == "core"
        and entry.kind == "LlmAudio"
        and entry.payload.get("to_state") == "SessionVisible"
        for entry in sink.entries
    )


def test_timeline_covers_connections_sessions_and_gemini_calls() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_FakeLiveConnect(),
            trace_sink=sink,
        )
        clock = AsyncioClock()
        app = App(
            transport=transport, gemini=gemini, clock=clock, trace_sink=sink
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with websockets.connect(_url(transport)) as ws:
                await register(ws, transport, client_name="desktop", drain=False)
                await ws.send(encode(SessionStart()))

                await wait_until(
                    lambda: any(
                        entry.source == "transport"
                        and entry.kind == "client_registered"
                        for entry in sink.entries
                    )
                )
                await wait_until(
                    lambda: any(
                        entry.source == "core"
                        and entry.kind == "SessionRequested"
                        for entry in sink.entries
                    )
                )
                await wait_until(
                    lambda: any(
                        entry.source == "gemini" and entry.kind == "connected"
                        for entry in sink.entries
                    )
                )

                from_state = sink.of_kind("SessionRequested")[-1].payload["from_state"]
                to_state = sink.of_kind("SessionRequested")[-1].payload["to_state"]
                assert from_state == "Idle"
                assert to_state == "LiveConnecting"
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        assert sink.of_kind("stopped")

    asyncio.run(scenario())


def test_stale_terminal_after_replacement_does_not_kill_the_new_session() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_TalkingLiveConnect(audio=b"reply"),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with websockets.connect(_url(transport)) as ws_a:
                await register(ws_a, transport, client_name="a", drain=False)
                await ws_a.send(encode(SessionStart()))
                assert decode(await asyncio.wait_for(ws_a.recv(), 1.0)) == (
                    SessionStarted(auto=False)
                )
                await wait_until(lambda: _session_visible(sink))

                # B taps while A's Session is still visible: the replacement
                # closes A's Live connection, depositing a stale LlmClosed(s1).
                async with websockets.connect(_url(transport)) as ws_b:
                    await register(ws_b, transport, client_name="b", drain=False)
                    await ws_b.send(encode(SessionStart()))

                    assert decode(await asyncio.wait_for(ws_b.recv(), 1.0)) == (
                        SessionStarted(auto=False)
                    )
                    assert decode(await asyncio.wait_for(ws_b.recv(), 1.0)) == Audio(
                        data=b"reply"
                    )
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        # A was replaced, not client-gone.
        reasons = [entry.payload["reason"] for entry in sink.of_kind("SessionClosed")]
        assert "REPLACED" in reasons

        # The stale terminal was traced and ignored: B's Session (s2) survived.
        stale = [
            entry
            for entry in sink.of_kind("LlmClosed")
            if entry.payload.get("stale_connection_id") == "s1"
        ]
        assert stale, "the stale terminal event was not traced"
        assert stale[-1].level == "debug"
        assert stale[-1].payload["connection_id"] == "s2"

    asyncio.run(scenario())


def test_active_client_disconnect_ends_the_session_with_client_gone() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_TalkingLiveConnect(audio=b"reply"),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with websockets.connect(_url(transport)) as ws_a:
                await register(ws_a, transport, client_name="a", drain=False)
                await ws_a.send(encode(SessionStart()))
                assert decode(await asyncio.wait_for(ws_a.recv(), 1.0)) == (
                    SessionStarted(auto=False)
                )
                await wait_until(lambda: _session_visible(sink))

            # A's socket closed: the core must end the Session, not leave a ghost.
            await wait_until(
                lambda: any(
                    entry.kind == "SessionClosed"
                    and entry.payload.get("reason") == "CLIENT_GONE"
                    for entry in sink.entries
                )
            )
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        closed = sink.of_kind("SessionClosed")
        assert closed[-1].payload["reason"] == "CLIENT_GONE"
        assert closed[-1].payload["to_state"] == "Idle"

    asyncio.run(scenario())


def test_client_disconnect_then_new_client_restarts_a_working_session() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_TalkingLiveConnect(audio=b"reply"),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with websockets.connect(_url(transport)) as ws_a:
                await register(ws_a, transport, client_name="a", drain=False)
                await ws_a.send(encode(SessionStart()))
                assert decode(await asyncio.wait_for(ws_a.recv(), 1.0)) == (
                    SessionStarted(auto=False)
                )
                await wait_until(lambda: _session_visible(sink))

            # A's socket closed: the core ends the Session and returns to Idle.
            await wait_until(
                lambda: any(
                    entry.kind == "SessionClosed"
                    and entry.payload.get("reason") == "CLIENT_GONE"
                    for entry in sink.entries
                )
            )

            # B taps afterwards: a clean restart, no ghost Session in the way.
            async with websockets.connect(_url(transport)) as ws_b:
                await register(ws_b, transport, client_name="b", drain=False)
                await ws_b.send(encode(SessionStart()))
                assert decode(await asyncio.wait_for(ws_b.recv(), 1.0)) == (
                    SessionStarted(auto=False)
                )
                assert decode(await asyncio.wait_for(ws_b.recv(), 1.0)) == Audio(
                    data=b"reply"
                )
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        # The previous Session ended on disconnect; B's Session survived.
        client_gone = [
            entry
            for entry in sink.of_kind("SessionClosed")
            if entry.payload["reason"] == "CLIENT_GONE"
        ]
        assert client_gone
        assert client_gone[-1].payload["to_state"] == "Idle"

    asyncio.run(scenario())


def test_pushed_event_opens_a_proactive_auto_session_for_the_registered_client() -> None:
    """Trace d'assemblage : un Événement poussé dans le Canal atteint le noyau,
    ouvre une Connexion Live de jugement, et une Annonce proactive `auto` part
    vers le Client de référence quand le LLM parle (kernel pur, adaptateur réel).
    """

    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_TalkingLiveConnect(audio=b"alerte"),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
            announce_prompt="JUGE",
            judge=FakeJudge(message="La porte est ouverte"),
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with websockets.connect(_url(transport)) as ws:
                await register(ws, transport, client_name="desktop", drain=False)
                # Wait for the kernel to register the client before pushing, so
                # the Annonce is addressed to a known client id.
                await wait_until(
                    lambda: any(
                        entry.source == "core"
                        and entry.kind == "ClientRegistered"
                        for entry in sink.entries
                    )
                )

                app.push_event("matrix", "porte ouverte")

                # La fenêtre de regroupement dure 5 s (ADR-0007) avant que le
                # noyau n'ouvre la Connexion Live de jugement : on la laisse
                # s'écouler.
                assert decode(await asyncio.wait_for(ws.recv(), 7.0)) == (
                    SessionStarted(auto=True)
                )
                assert decode(await asyncio.wait_for(ws.recv(), 1.0)) == Audio(
                    data=b"alerte"
                )
                await wait_until(lambda: bool(sink.of_kind("EventJudge")))
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        judge = sink.of_kind("EventJudge")[-1]
        assert judge.source == "core"
        assert judge.payload["events"] == 1

    asyncio.run(scenario())


FIXTURE_INSTRUCTION = "Utilise fixture__echo pour répéter."

PLUGIN_CODE = '''
from plugin_api import PluginBinding, PluginUnavailable, ToolBinding, ToolSpec

PARAMETERS = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
}
DESCRIPTION = "Écho du plugin de fixture"
INSTRUCTION = "Utilise fixture__echo pour répéter."


async def handle(arguments):
    return {"status": "ok", "echo": arguments.get("value", "")}


def create(context):
    return PluginBinding(
        name="fixture",
        tools=(
            ToolBinding(
                spec=ToolSpec(
                    name="fixture__echo",
                    description=DESCRIPTION,
                    parameters=PARAMETERS,
                ),
                handler=handle,
            ),
        ),
        instruction=INSTRUCTION,
    )
'''


class _ScriptedSession:
    """Fake Live session : le test pousse les réponses, l'adaptateur les lit."""

    def __init__(self) -> None:
        self._responses: asyncio.Queue[Any] = asyncio.Queue()
        self.function_responses: list[Any] = []

    async def send_realtime_input(self, **kwargs: Any) -> None:
        pass

    async def send_client_content(self, **kwargs: Any) -> None:
        pass

    async def send_tool_response(self, function_responses: Any) -> None:
        self.function_responses.append(function_responses)

    async def receive(self) -> Any:
        yield await self._responses.get()

    def call_tool(self, tool_id: str, name: str, arguments: dict) -> None:
        call = SimpleNamespace(id=tool_id, name=name, args=arguments)
        response = SimpleNamespace(
            tool_call=SimpleNamespace(function_calls=[call])
        )
        self._responses.put_nowait(response)


class _ScriptedContext:
    def __init__(self, session: _ScriptedSession) -> None:
        self._session = session

    async def __aenter__(self) -> _ScriptedSession:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _ToolLiveConnect:
    """Fake ``live_connect`` : retient les configs et les sessions ouvertes."""

    def __init__(self) -> None:
        self.configs: list[Any] = []
        self.sessions: list[_ScriptedSession] = []

    def __call__(self, model: str, config: Any) -> _ScriptedContext:
        session = _ScriptedSession()
        self.configs.append(config)
        self.sessions.append(session)
        return _ScriptedContext(session)

    @property
    def session(self) -> _ScriptedSession:
        return self.sessions[-1]

    def declarations(self) -> list[Any]:
        tools = self.configs[-1].tools or []
        return [decl for tool in tools for decl in (tool.function_declarations or [])]

    def instruction(self) -> str:
        content = self.configs[-1].system_instruction
        if content is None:
            return ""
        return "\n".join(part.text or "" for part in content.parts)


def _install_fixture_plugin(tmp_path, code: str = PLUGIN_CODE) -> tuple:
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "fixture"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "api_version": 1,
                "name": "fixture",
                "version": "0.1.0",
                "description": "Plugin de fixture",
                "platform": "server",
                "entrypoint": "plugin.py",
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text(code, encoding="utf-8")
    state_path = tmp_path / "plugins.json"
    state_path.write_text(
        json.dumps({"enabled": ["fixture"], "config": {}}), encoding="utf-8"
    )
    return plugins_dir, state_path


def test_plugin_tool_is_declared_called_and_answered_on_the_live_connection(
    tmp_path,
) -> None:
    async def scenario() -> None:
        plugins_dir, state_path = _install_fixture_plugin(tmp_path)
        sink = RecordingTraceSink()
        plugins = PluginRegistry()
        transport = WebSocketTransport(
            host="127.0.0.1",
            port=0,
            server_tools=server_tool_names(plugins),
            trace_sink=sink,
        )
        live = _ToolLiveConnect()
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=live,
            trace_sink=sink,
        )
        loader = PluginLoader(
            directory=plugins_dir,
            state_path=state_path,
            secrets=EnvSecretResolver({}),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
            plugins=plugins,
            plugin_loader=loader,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            # Le chargement des plugins précède le démarrage du transport et
            # la trace ``started``.
            kinds = [entry.kind for entry in sink.entries]
            state_index = next(
                i for i, entry in enumerate(sink.entries)
                if entry.kind == "PluginState"
            )
            assert state_index < kinds.index("started")
            assert sink.of_kind("PluginState")[0].payload["state"] == "activated"

            async with websockets.connect(_url(transport)) as ws:
                registered = await register(ws, transport, drain=False)
                # ``Registered.server_tools`` liste ce que la Connexion Live
                # déclarera : le Tool du plugin est déjà chargé et déclaré.
                assert registered.server_tools == ["fixture__echo"]
                await ws.send(encode(SessionStart()))
                await wait_until(lambda: bool(live.sessions))

                # Les Tools du plugin sont déclarés tels quels : nom,
                # description et schéma d'origine, sans préfixe Athena.
                [declaration] = live.declarations()
                assert registered.server_tools == [
                    declared.name for declared in live.declarations()
                ]
                # L'Instruction de plugin est composée dans le prompt système
                # de la même Connexion que les Tools qu'elle décrit.
                assert FIXTURE_INSTRUCTION in live.instruction()
                assert declaration.name == "fixture__echo"
                assert declaration.description == "Écho du plugin de fixture"
                assert dict(declaration.parameters_json_schema) == {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                }

                live.session.call_tool("t1", "fixture__echo", {"value": "bonjour"})
                await wait_until(lambda: bool(live.session.function_responses))

                response = live.session.function_responses[0]
                assert response.id == "t1"
                assert response.name == "fixture__echo"
                assert dict(response.response) == {
                    "status": "ok",
                    "echo": "bonjour",
                }

                # Un nom inconnu rend l'erreur conventionnelle au modèle sans
                # faire tomber la Session : l'appel suivant passe encore.
                live.session.call_tool("t2", "ghost", {})
                await wait_until(lambda: len(live.session.function_responses) == 2)
                error = live.session.function_responses[1]
                assert dict(error.response) == {
                    "status": "error",
                    "message": "unknown tool: ghost",
                }

                live.session.call_tool("t3", "fixture__echo", {"value": "encore"})
                await wait_until(lambda: len(live.session.function_responses) == 3)
                assert dict(live.session.function_responses[2].response) == {
                    "status": "ok",
                    "echo": "encore",
                }

                # Chaque appel est tracé avec son plugin propriétaire.
                calls = sink.of_kind("ToolCall")
                assert [call.payload["plugin"] for call in calls] == [
                    "fixture",
                    None,
                    "fixture",
                ]
                assert sink.of_kind("ToolResult")[1].payload["status"] == "error"
                assert not sink.of_kind("SessionClosed")
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

    asyncio.run(scenario())


FLAKY_PLUGIN_CODE = PLUGIN_CODE.replace(
    "def create(context):\n    return PluginBinding(",
    '''_attempts = 0


def create(context):
    global _attempts
    _attempts += 1
    if _attempts == 1:
        raise PluginUnavailable("home assistant injoignable")
    return PluginBinding(''',
)


def test_a_failed_plugin_is_retried_and_its_tools_arrive_on_the_next_connection(
    tmp_path,
) -> None:
    """Démarrage dégradé : la panne est tracée, la retente répare sans redémarrage."""

    async def scenario() -> None:
        plugins_dir, state_path = _install_fixture_plugin(
            tmp_path, code=FLAKY_PLUGIN_CODE
        )
        sink = RecordingTraceSink()
        plugins = PluginRegistry()
        transport = WebSocketTransport(
            host="127.0.0.1",
            port=0,
            server_tools=server_tool_names(plugins),
            trace_sink=sink,
        )
        live = _ToolLiveConnect()
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=live,
            trace_sink=sink,
        )
        loader = PluginLoader(
            directory=plugins_dir,
            state_path=state_path,
            secrets=EnvSecretResolver({}),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
            plugins=plugins,
            plugin_loader=loader,
            plugin_retry_interval=0.01,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            # Le serveur a démarré malgré la panne, et le plugin est en échec.
            [failed] = sink.of_kind("PluginState")
            assert failed.payload["state"] == "failed"
            assert "PluginUnavailable" in failed.payload["reason"]
            assert plugins.tools.specs() == ()

            # Première Connexion Live : le plugin est absent des Tools déclarés,
            # donc ``Registered.server_tools`` ne l'annonce pas non plus.
            async with websockets.connect(_url(transport)) as ws_a:
                registered_a = await register(
                    ws_a, transport, client_name="a", drain=False
                )
                assert registered_a.server_tools == []
                await ws_a.send(encode(SessionStart()))
                await wait_until(lambda: bool(live.sessions))
                assert live.declarations() == []
                assert live.instruction() == ""

            # La déconnexion rend le noyau Idle, puis la retente de fond répare.
            await wait_until(
                lambda: any(
                    entry.kind == "SessionClosed"
                    and entry.payload.get("reason") == "CLIENT_GONE"
                    for entry in sink.entries
                )
            )
            await wait_until(
                lambda: any(
                    entry.kind == "PluginState"
                    and entry.payload["state"] == "activated"
                    for entry in sink.entries
                )
            )
            assert plugins.tools.owner_of("fixture__echo") == "fixture"
            assert not runner.done()

            # Connexion Live suivante, sans redémarrage : le Tool est là et sert,
            # et l'enregistrement du Client l'annonce au passage.
            async with websockets.connect(_url(transport)) as ws_b:
                registered_b = await register(
                    ws_b, transport, client_name="b", drain=False
                )
                assert registered_b.server_tools == ["fixture__echo"]
                await ws_b.send(encode(SessionStart()))
                await wait_until(lambda: len(live.sessions) == 2)

                [declaration] = live.declarations()
                assert declaration.name == "fixture__echo"
                assert registered_b.server_tools == [
                    declared.name for declared in live.declarations()
                ]
                assert FIXTURE_INSTRUCTION in live.instruction()

                live.session.call_tool("t1", "fixture__echo", {"value": "bonjour"})
                await wait_until(lambda: bool(live.session.function_responses))
                assert dict(live.session.function_responses[0].response) == {
                    "status": "ok",
                    "echo": "bonjour",
                }

            assert len(sink.of_kind("started")) == 1
            assert not sink.of_kind("stopped")
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        assert loader.pending == ()

    asyncio.run(scenario())


def test_a_retry_that_keeps_failing_never_crashes_the_running_server(
    tmp_path,
) -> None:
    async def scenario() -> None:
        code = '''from plugin_api import PluginUnavailable


def create(context):
    raise PluginUnavailable("toujours injoignable")
'''
        plugins_dir, state_path = _install_fixture_plugin(tmp_path, code=code)
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_ToolLiveConnect(),
            trace_sink=sink,
        )
        plugins = PluginRegistry()
        loader = PluginLoader(
            directory=plugins_dir,
            state_path=state_path,
            secrets=EnvSecretResolver({}),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
            plugins=plugins,
            plugin_loader=loader,
            plugin_retry_interval=0.01,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(
                lambda: len(
                    [
                        entry
                        for entry in sink.of_kind("PluginState")
                        if entry.payload["state"] == "failed"
                    ]
                )
                >= 3
            )
            assert not runner.done()
            assert loader.pending == ("fixture",)
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        assert sink.of_kind("stopped")

    asyncio.run(scenario())


def test_a_listed_plugin_without_a_directory_is_missing_and_keeps_its_state_line(
    tmp_path,
) -> None:
    async def scenario() -> None:
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        state_path = tmp_path / "plugins.json"
        state_path.write_text(
            json.dumps(
                {
                    "enabled": ["homeassistant"],
                    "config": {"homeassistant": {"url": "http://maison:8123"}},
                }
            ),
            encoding="utf-8",
        )
        before = state_path.read_bytes()

        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_ToolLiveConnect(),
            trace_sink=sink,
        )
        plugins = PluginRegistry()
        loader = PluginLoader(
            directory=plugins_dir,
            state_path=state_path,
            secrets=EnvSecretResolver({}),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
            plugins=plugins,
            plugin_loader=loader,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            [state] = sink.of_kind("PluginState")
            assert state.source == "plugins"
            assert state.payload == {"plugin": "homeassistant", "state": "missing"}
            assert plugins.tools.specs() == ()
            assert loader.pending == ()
            # Le chargeur ne réécrit jamais le fichier d'état : la ligne du
            # plugin absent est conservée pour une réinstallation.
            assert state_path.read_bytes() == before
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        assert sink.of_kind("stopped")
        assert state_path.read_bytes() == before

    asyncio.run(scenario())
