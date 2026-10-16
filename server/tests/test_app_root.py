from __future__ import annotations

import asyncio
import logging
from typing import Callable, TypeVar

from conftest import FakeJudge, RecordingTraceSink, wait_until
from protocol import SessionEndReason
from server.app.config import AppConfig, GeminiConfig
from server.app.root import App, build_app, load_announce_prompt, load_judge_prompt
from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    CloseLiveConnection,
    Command,
    OpenLiveConnection,
    SendAudioToClient,
    SendAudioToLlm,
    SendAnnouncementToLlm,
    SendMemoryToLlm,
    SendToolResultToLlm,
    StartIdleTimer,
)
from server.core.events import (
    ClientAudio,
    ClientRegistered,
    Event,
    LlmAudio,
    LlmOpened,
    LlmToolCall,
    LlmTranscription,
    Role,
    SessionRequested,
    TimerFired,
)
from server.core.ports import MemoryHit
from server.core.tools import ToolRegistry
from server.core.memory_tools import register_memory_tools
from server.core.coordinator import (
    EVENT_WINDOW_TIMER_ID,
    EVENT_WINDOW_TIMEOUT,
    ANNOUNCE_TIMEOUT,
)

_CommandT = TypeVar("_CommandT", bound=Command)


class FakeAdapter:
    """Adaptateur factice exposant le même seam que transport/gemini."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.commands: list[Command] = []
        self.tool_results: list[tuple[str, str, str, object]] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def recv(self) -> Event:
        return await self.events.get()

    async def execute(self, command: Command) -> None:
        self.commands.append(command)
        if isinstance(command, SendToolResultToLlm):
            self.tool_results.append(
                (command.connection_id, command.tool_id, command.name, command.result)
            )

    async def send_tool_result(
        self, connection_id: str, tool_id: str, name: str, result: object
    ) -> None:
        self.tool_results.append((connection_id, tool_id, name, result))

    async def feed(self, event: Event) -> None:
        await self.events.put(event)

    def of_type(self, command_type: type[_CommandT]) -> list[_CommandT]:
        return [c for c in self.commands if isinstance(c, command_type)]


class FakeClock:
    def __init__(self) -> None:
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.scheduled: list[tuple[str, float]] = []
        self.cancelled: list[str] = []
        self.closed = False

    def schedule(self, connection_id: str, timeout: float) -> None:
        self.scheduled.append((connection_id, timeout))

    def cancel(self, connection_id: str) -> None:
        self.cancelled.append(connection_id)

    async def recv(self) -> Event:
        return await self.events.get()

    async def fire(self, connection_id: str) -> None:
        await self.events.put(TimerFired(connection_id=connection_id))

    def close(self) -> None:
        self.closed = True


class FakeWebhook:
    """Adaptateur Webhook factice : retient le push et son cycle de vie."""

    def __init__(self) -> None:
        self.push: Callable[[str, str], None] | None = None
        self.started = False
        self.stopped = False

    async def start(self, push: Callable[[str, str], None]) -> None:
        self.push = push
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class FakeMemory:
    def __init__(self, hits: list[MemoryHit] | None = None) -> None:
        self._hits = list(hits or [])
        self.turns: list[tuple[str, str]] = []
        self.queries: list[str] = []
        self.saved: list[tuple[str, str, bool]] = []
        self.forgotten: list[str] = []
        self._pinned: list[str] = []

    async def session_context(self, user_id: str) -> str:
        return "\n".join(self._pinned)

    async def pinned_facts(self, user_id: str) -> list[str]:
        return list(self._pinned)

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        self.turns.append((role, text))

    async def recall(self, user_id: str, query: str) -> list[MemoryHit]:
        self.queries.append(query)
        return list(self._hits)

    async def save(self, user_id: str, content: str, *, pinned: bool) -> None:
        self.saved.append((user_id, content, pinned))
        if pinned:
            self._pinned.append(content)

    async def forget(self, user_id: str, query: str, max_distance: float) -> bool:
        self.forgotten.append(query)
        for index, content in enumerate(self._pinned):
            if query in content:
                del self._pinned[index]
                return True
        return False


def _build(
    memory=None, tools=None, announce_prompt="", judge=None, webhook=None
) -> tuple[FakeAdapter, FakeAdapter, FakeClock, RecordingTraceSink, App]:
    transport = FakeAdapter()
    gemini = FakeAdapter()
    clock = FakeClock()
    sink = RecordingTraceSink()
    app = App(
        transport=transport,
        gemini=gemini,
        clock=clock,
        trace_sink=sink,
        memory=memory,
        user_id="alice",
        tools=tools,
        announce_prompt=announce_prompt,
        judge=judge,
        webhook=webhook,
    )
    return transport, gemini, clock, sink, app


async def _open_visible_session(
    transport: FakeAdapter, gemini: FakeAdapter
) -> None:
    await transport.feed(ClientRegistered(client_id="c1"))
    await transport.feed(SessionRequested(client_id="c1"))
    await gemini.feed(LlmOpened())
    await gemini.feed(LlmAudio(b"hi"))
    await wait_until(lambda: transport.of_type(AnnounceSessionStarted))


def test_run_starts_transport_and_fans_in_both_adapters() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)

            await transport.feed(ClientRegistered(client_id="c1"))
            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))
            await wait_until(lambda: clock.scheduled)

            assert any(e.kind == "SessionRequested" for e in sink.entries)

            await gemini.feed(LlmOpened())
            await gemini.feed(LlmAudio(b"hello"))
            await wait_until(lambda: transport.of_type(AnnounceSessionStarted))
            assert transport.of_type(SendAudioToClient)[0].data == b"hello"

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_client_audio_during_a_live_session_goes_to_the_llm() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(ClientRegistered(client_id="c1"))
            await transport.feed(SessionRequested(client_id="c1"))
            await gemini.feed(LlmOpened())
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))

            await transport.feed(ClientAudio(data=b"mic"))
            await wait_until(lambda: gemini.of_type(SendAudioToLlm))
            assert gemini.of_type(SendAudioToLlm)[0].data == b"mic"

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_user_utterance_routes_recall_injection_to_the_llm() -> None:
    async def scenario() -> None:
        memory = FakeMemory(hits=[MemoryHit("bleu", 0.2, "journal")])
        transport, gemini, clock, sink, app = _build(memory)
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(SessionRequested(client_id="c1"))
            await gemini.feed(LlmOpened())

            await gemini.feed(LlmTranscription(role=Role.USER, text="salut"))

            await wait_until(lambda: gemini.of_type(SendMemoryToLlm))
            command = gemini.of_type(SendMemoryToLlm)[0]
            assert command.text == "[MÉMOIRE]\n- bleu"
            assert memory.turns == [("user", "salut")]
            assert memory.queries == ["salut"]

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_tool_result_is_routed_to_gemini_send_tool_result() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(ClientRegistered(client_id="c1"))
            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))
            connection_id = gemini.of_type(OpenLiveConnection)[0].connection_id
            await gemini.feed(LlmOpened())

            await gemini.feed(
                LlmToolCall(tool_id="t1", name="echo", arguments={"value": "x"})
            )
            await wait_until(lambda: gemini.tool_results)

            routed_connection, tool_id, name, _ = gemini.tool_results[0]
            assert routed_connection == connection_id
            assert tool_id == "t1"
            assert name == "echo"

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_clock_timeout_tears_the_session_down() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(ClientRegistered(client_id="c1"))
            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))
            connection_id = gemini.of_type(OpenLiveConnection)[0].connection_id
            await gemini.feed(LlmOpened())
            await gemini.feed(LlmAudio(b"hi"))
            await wait_until(lambda: transport.of_type(AnnounceSessionStarted))

            await clock.fire(connection_id)
            await wait_until(lambda: gemini.of_type(CloseLiveConnection))

            ended = transport.of_type(AnnounceSessionEnded)
            assert ended and ended[0].reason is SessionEndReason.TIMEOUT
            assert connection_id in clock.cancelled

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_idle_timer_is_armed_and_rearmed_through_the_clock() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: clock.scheduled)
            first_connection = clock.scheduled[0][0]

            await gemini.feed(LlmOpened())
            await gemini.feed(LlmAudio(b"one"))
            await wait_until(lambda: len(clock.scheduled) >= 2)
            assert all(key == first_connection for key, _ in clock.scheduled)

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_shutdown_requested_runs_teardown_with_shutdown_reason() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)
        await _open_visible_session(transport, gemini)

        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        ended = transport.of_type(AnnounceSessionEnded)
        assert ended and ended[-1].reason is SessionEndReason.SHUTDOWN
        assert gemini.of_type(CloseLiveConnection)
        assert clock.scheduled[0][0] in clock.cancelled
        assert transport.stopped
        assert clock.closed
        assert any(
            entry.payload.get("reason") == "SHUTDOWN" for entry in sink.entries
        )

    asyncio.run(scenario())


def test_shutdown_while_idle_stops_cleanly() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)

        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        assert transport.stopped
        assert clock.closed

    asyncio.run(scenario())


def test_run_leaves_no_orphan_task() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)

        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        assert pending == []

    asyncio.run(scenario())


def test_shutdown_requested_is_traced_even_when_idle() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)

        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        assert any(e.kind == "ShutdownRequested" for e in sink.entries)

    asyncio.run(scenario())


def test_build_app_installs_logging_bridge_debug_only_when_flag_on() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    gemini = GeminiConfig(api_key="test-key")
    try:
        build_app(AppConfig(gemini=gemini, debug_decisions=True))
        assert root.level == logging.DEBUG

        build_app(AppConfig(gemini=gemini, debug_decisions=False, log_level="WARNING"))
        assert root.level == logging.WARNING
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


def test_build_app_wires_the_kernel_memory_tools() -> None:
    app = build_app(AppConfig(gemini=GeminiConfig(api_key="test-key")))

    assert app.tools is not None
    assert {spec.name for spec in app.tools.specs()} == {"save_memory", "forget_memory"}


def test_save_memory_tool_call_reaches_the_store_and_the_model() -> None:
    async def scenario() -> None:
        memory = FakeMemory()
        registry = ToolRegistry()
        register_memory_tools(registry, memory, "alice")
        transport, gemini, clock, sink, app = _build(memory, tools=registry)
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))
            opened = gemini.of_type(OpenLiveConnection)[0]
            assert {spec.name for spec in opened.tools} == {
                "save_memory",
                "forget_memory",
            }
            await gemini.feed(LlmOpened())

            await gemini.feed(
                LlmToolCall(
                    tool_id="t1",
                    name="save_memory",
                    arguments={"content": "aime le bleu", "kind": "profile"},
                )
            )
            await wait_until(lambda: gemini.tool_results)

            assert memory.saved == [("alice", "aime le bleu", True)]
            _, tool_id, name, result = gemini.tool_results[0]
            assert (tool_id, name) == ("t1", "save_memory")
            assert result["status"] == "ok"

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_pushed_event_at_rest_judges_then_opens_an_announcement_connection() -> None:
    async def scenario() -> None:
        judge = FakeJudge(message="La porte est ouverte")
        transport, gemini, clock, sink, app = _build(
            announce_prompt="JUGE", judge=judge
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(ClientRegistered(client_id="c1"))

            app.push_event("matrix", "porte ouverte")

            # La fenêtre de regroupement s'arme d'abord : aucune Connexion Live
            # n'est ouverte avant l'échéance du timer réservé.
            await wait_until(
                lambda: (EVENT_WINDOW_TIMER_ID, EVENT_WINDOW_TIMEOUT)
                in clock.scheduled
            )
            assert not gemini.of_type(OpenLiveConnection)

            await clock.fire(EVENT_WINDOW_TIMER_ID)

            await wait_until(lambda: gemini.of_type(OpenLiveConnection))
            opened = gemini.of_type(OpenLiveConnection)[0]
            assert opened.instruction == "JUGE"
            assert opened.tools == ()
            # Le jugement est **texte** : les faits étiquetés arrivent au juge,
            # et seule la Connexion d'Annonce ouvre pour faire parler le verdict.
            assert judge.calls[-1] == ["[matrix] porte ouverte"]

            await wait_until(lambda: gemini.of_type(SendAnnouncementToLlm))
            announcement = gemini.of_type(SendAnnouncementToLlm)[0]
            assert announcement.connection_id == opened.connection_id
            assert announcement.text == "La porte est ouverte"
            assert clock.scheduled[-1] == (opened.connection_id, ANNOUNCE_TIMEOUT)

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_load_judge_prompt_reads_the_versioned_file() -> None:
    prompt = load_judge_prompt()

    assert prompt.strip()
    assert prompt == load_judge_prompt()


def test_load_announce_prompt_reads_the_versioned_file() -> None:
    prompt = load_announce_prompt()

    assert prompt.strip()
    assert prompt == load_announce_prompt()


def test_run_serves_the_webhook_and_binds_push_event() -> None:
    async def scenario() -> None:
        hook = FakeWebhook()
        transport, gemini, clock, sink, app = _build(webhook=hook)
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: hook.started)
            await transport.feed(ClientRegistered(client_id="c1"))

            assert hook.push is not None
            hook.push("matrix", "porte ouverte")
            await wait_until(lambda: any(e.kind == "EventQueued" for e in sink.entries))

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
            assert hook.stopped
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())

