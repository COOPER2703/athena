from __future__ import annotations

from enum import StrEnum
from typing import Mapping

from protocol import SessionEndReason

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
    Trace,
)
from server.core.consolidation import RECENT_EVENT_ROLE
from server.core.events import (
    ClientAudio,
    ClientDisconnected,
    ClientInterrupt,
    ClientRegistered,
    ClientToolResult,
    Event,
    EventReceived,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmOpened,
    LlmToolCall,
    LlmTranscription,
    Role,
    SessionRequested,
    ShutdownRequested,
    TimerFired,
)
from server.core.ports import EventJudge, Judgment, MemoryStore, ToolSpec
from server.core.state import SessionState
from server.core.tools import ToolRegistry

_TRACE_SOURCE = "core"
IDLE_TIMEOUT = 30.0
# Filet de sécurité de l'Annonce (ADR-0007, amendé) : une Connexion Live n'est
# ouverte que si le jugement **texte** a décidé d'annoncer, mais si le modèle ne
# produit aucun son, elle est refermée vite — plus court que l'inactivité d'une
# Session — sans qu'aucune Session n'apparaisse.
ANNOUNCE_TIMEOUT = 5.0
# Fenêtre de regroupement **paresseuse et fixe** (ADR-0007) : ouverte au premier
# Événement reçu au Repos, elle dure 5 s sans être relancée par les arrivées
# suivantes, puis juge le lot entier en un seul tour.
EVENT_WINDOW_TIMEOUT = 5.0
# Clé réservée de la fenêtre : elle réutilise la couture ``Clock`` existante par
# les commandes de timer, sans changer la forme du port.
EVENT_WINDOW_TIMER_ID = "event-window"
# Plafond de la File d'attente d'Événements (ADR-0007) : il porte sur la File
# **seule**, pas de quota par Source. Au-delà, les plus anciens sont jetés à
# l'arrivée et chaque drop est tracé.
EVENT_QUEUE_MAX = 120
# Taille maximale d'un Événement : un contenu plus long est rejeté à l'arrivée
# (jamais mis en File) et le rejet est tracé.
EVENT_MAX_CHARS = 4096
_MEMORY_HEADER = "[MÉMOIRE]"
_EVENT_HEADER = "[ÉVÉNEMENTS]"


class EventDropReason(StrEnum):
    """Cause d'un rejet d'Événement, portée par une trace ``EventQueueDropped``.

    ``TOO_LARGE`` (contenu au-delà de ``EVENT_MAX_CHARS``), ``DUPLICATE``
    (répétition exacte ``(source, contenu)``), ``CAPACITY`` (plus ancien jeté
    au-delà de ``EVENT_QUEUE_MAX``), ``SHUTDOWN`` (File éphémère vidée à
    l'arrêt) et ``JUDGMENT_ABORTED`` (lot d'un jugement en vol dont la
    Connexion Live s'est terminée anormalement).
    """

    TOO_LARGE = "too_large"
    DUPLICATE = "duplicate"
    CAPACITY = "capacity"
    SHUTDOWN = "shutdown"
    JUDGMENT_ABORTED = "judgment_aborted"


class Coordinator:
    """Pure, deterministic event -> command transition function.

    Holds no direct I/O, no clock and imports no asyncio: it receives domain
    events and awaits only the async ports injected by the composition root
    (``MemoryStore`` today), then returns the commands to carry out.
    """

    def __init__(
        self,
        *,
        memory: MemoryStore | None = None,
        user_id: str = "owner",
        system_prompt: str = "",
        announce_prompt: str = "",
        judge: EventJudge | None = None,
        tools: ToolRegistry | None = None,
        recall_k: int = 5,
        recall_window_turns: int = 3,
        recall_max_distance: float = 0.6,
    ) -> None:
        self._memory = memory
        self._user_id = user_id
        self._system_prompt = system_prompt
        self._announce_prompt = announce_prompt
        self._judge = judge
        self._tools = tools  # Tool registry seam (issue #28).
        self._recall_k = recall_k
        self._recall_window_turns = recall_window_turns
        self._recall_max_distance = recall_max_distance
        self._state = SessionState.IDLE
        self._client_id = ""
        self._connection_id = ""
        self._session_id = ""
        self._visible = False
        self._auto = False
        self._judging = False
        self._session_counter = 0
        self._audio_buffer: list[bytes] = []
        self._recent_utterances: list[str] = []
        self._injected_memories: set[str] = set()
        # Identité du Client de référence (Annonce proactive) et File d'attente
        # d'Événements (état du noyau, pas une seconde file — ADR-0010).
        self._registered_client_id = ""
        self._event_queue: list[EventReceived] = []
        self._window_open = False
        # Le lot jugé, gardé jusqu'à l'Annonce pour ne le journaliser qu'au
        # moment où la Connexion de jugement devient visible (ticket #42).
        self._judged_batch: list[EventReceived] = []
        # Tour d'annonce en cours : tant qu'il tient, les transcriptions de
        # l'assistant sont écrites au Journal en rôle ``event`` (la parole qui
        # annonce), pas en ``assistant``. Il se referme au premier tour
        # utilisateur, ou avec la Session.
        self._announcing = False

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @property
    def session_id(self) -> str:
        return self._session_id

    async def handle(self, event: Event) -> list[Command]:
        commands = await self._dispatch(event)
        # Modèle unifié : dès que le noyau est au Repos avec des Événements en
        # attente et aucune fenêtre ouverte, la fenêtre de regroupement s'ouvre.
        # Le même chemin couvre le premier Événement reçu au Repos et les
        # Événements mis en File pendant une Session, jugés au retour au Repos.
        commands.extend(self._open_window_if_needed())
        return commands

    async def _dispatch(self, event: Event) -> list[Command]:
        if isinstance(event, ClientRegistered):
            return self._on_client_registered(event)
        if isinstance(event, SessionRequested):
            return await self._on_session_requested(event)
        if isinstance(event, ClientAudio):
            return self._on_client_audio(event)
        if isinstance(event, ClientDisconnected):
            return self._on_client_disconnected(event)
        if isinstance(event, ClientInterrupt):
            return self._on_client_interrupt(event)
        if isinstance(event, ClientToolResult):
            return self._on_client_tool_result(event)
        if isinstance(event, LlmOpened):
            return self._on_llm_opened(event)
        if isinstance(event, LlmAudio):
            return await self._on_llm_audio(event)
        if isinstance(event, LlmTranscription):
            return await self._on_llm_transcription(event)
        if isinstance(event, LlmToolCall):
            return await self._on_llm_tool_call(event)
        if isinstance(event, LlmClosed):
            return self._on_llm_closed(event)
        if isinstance(event, LlmFailed):
            return self._on_llm_failed(event)
        if isinstance(event, ShutdownRequested):
            return self._on_shutdown_requested(event)
        if isinstance(event, TimerFired):
            return await self._on_timer_fired(event)
        if isinstance(event, EventReceived):
            return self._on_event_received(event)
        raise TypeError(f"Unsupported event: {type(event).__name__}")

    def _on_client_registered(self, event: ClientRegistered) -> list[Command]:
        self._registered_client_id = event.client_id
        return self._decision("ClientRegistered", client_id=event.client_id)

    def _on_event_received(self, event: EventReceived) -> list[Command]:
        """Un Événement reçu rejoint la File après les gardes d'assainissement.

        Ordre des gardes à l'arrivée, fixé et épinglé par les tests :
        **taille** (un contenu trop volumineux est rejeté avant tout état) →
        **dédup** (un doublon exact ``(source, contenu)`` de la fenêtre courante
        n'est pas jugé deux fois) → **plafond** (au-delà de ``EVENT_QUEUE_MAX``,
        les plus anciens sont jetés). Chaque rejet émet une trace
        ``EventQueueDropped`` portant sa ``reason``.

        Au Repos, ``handle`` ouvre la fenêtre si elle ne l'est pas déjà (premier
        Événement) ; les arrivées suivantes rejoignent le lot sans relancer la
        fenêtre. Hors Repos, l'Événement attend le retour au Repos ; les
        doublons accumulés y sont écrasés à l'ouverture de la fenêtre.
        """
        if len(event.content) > EVENT_MAX_CHARS:
            return [self._event_drop_trace(event, EventDropReason.TOO_LARGE)]
        if self._window_collects() and self._is_duplicate(event):
            return [self._event_drop_trace(event, EventDropReason.DUPLICATE)]
        self._event_queue.append(event)
        commands: list[Command] = [
            self._trace(
                "EventQueued",
                from_state=self._state,
                to_state=self._state,
                client_id=self._registered_client_id,
                level="debug",
                extra={"source": event.source, "queued": len(self._event_queue)},
            )
        ]
        commands.extend(self._enforce_ceiling())
        return commands

    def _enforce_ceiling(self) -> list[Command]:
        """Jette les plus anciens tant que la File dépasse ``EVENT_QUEUE_MAX``."""
        commands: list[Command] = []
        while len(self._event_queue) > EVENT_QUEUE_MAX:
            dropped = self._event_queue.pop(0)
            commands.append(self._event_drop_trace(dropped, EventDropReason.CAPACITY))
        return commands

    def _event_drop_trace(
        self, event: EventReceived, reason: EventDropReason
    ) -> Trace:
        """Trace le rejet d'un Événement avec sa cause (jamais jugé)."""
        return self._trace(
            "EventQueueDropped",
            from_state=self._state,
            to_state=self._state,
            client_id=self._registered_client_id,
            reason=reason.value,
            extra={"source": event.source},
        )

    def _window_collects(self) -> bool:
        """Vrai quand une fenêtre collecte activement des Événements au Repos.

        Hors Repos, la fenêtre perd son sens : les arrivées sont mises en File
        sans dédup, et les répétitions seront écrasées à la réouverture.
        """
        return self._window_open and self._state is SessionState.IDLE

    @staticmethod
    def _event_key(event: EventReceived) -> tuple[str, str]:
        """Clé d'identité d'un Événement : paire exacte ``(source, contenu)``."""
        return (event.source, event.content)

    @staticmethod
    def _collapse(
        events: list[EventReceived],
    ) -> tuple[list[EventReceived], list[EventReceived]]:
        """Sépare une liste en ``(uniques conservés, doublons écartés)``.

        Le premier Événement de chaque paire ``(source, contenu)`` est conservé,
        dans l'ordre d'arrivée ; toute occurrence suivante est un doublon.
        """
        seen: set[tuple[str, str]] = set()
        kept: list[EventReceived] = []
        dropped: list[EventReceived] = []
        for event in events:
            key = Coordinator._event_key(event)
            if key in seen:
                dropped.append(event)
                continue
            seen.add(key)
            kept.append(event)
        return kept, dropped

    def _is_duplicate(self, event: EventReceived) -> bool:
        """Doublon exact ``(source, contenu)`` déjà collecté dans la fenêtre."""
        _, dropped = self._collapse([*self._event_queue, event])
        return bool(dropped)

    def _collapse_window_duplicates(self) -> list[Command]:
        """Écarte les doublons entrés en fenêtre, en gardant le plus ancien.

        Les Événements accumulés hors Repos peuvent contenir des répétitions :
        à l'ouverture de la fenêtre, seule la première occurrence de chaque
        paire exacte ``(source, contenu)`` est conservée, dans l'ordre d'arrivée
        ; chaque occurrence écartée est tracée (raison ``duplicate``).
        """
        kept, dropped = self._collapse(self._event_queue)
        self._event_queue = kept
        return [
            self._event_drop_trace(event, EventDropReason.DUPLICATE)
            for event in dropped
        ]

    def _open_window_if_needed(self) -> list[Command]:
        """Ouvre la fenêtre de regroupement au Repos, une seule fois par lot.

        Paresseuse : armée par le premier Événement en attente, elle émet un
        ``StartIdleTimer`` sur la **clé réservée** et n'est pas relancée par les
        arrivées suivantes ; l'échéance juge le lot d'un bloc.
        """
        if (
            self._state is not SessionState.IDLE
            or self._window_open
            or not self._event_queue
        ):
            return []
        self._window_open = True
        commands = self._collapse_window_duplicates()
        commands.append(
            StartIdleTimer(
                connection_id=EVENT_WINDOW_TIMER_ID,
                timeout=EVENT_WINDOW_TIMEOUT,
            )
        )
        commands.append(
            self._trace(
                "EventWindowOpened",
                from_state=self._state,
                to_state=self._state,
                client_id=self._registered_client_id,
                extra={"events": len(self._event_queue)},
            )
        )
        return commands

    def _cancel_window(self) -> list[Command]:
        """Annule la fenêtre ouverte sans toucher la File d'attente.

        L'utilisateur gagne toujours (ADR-0007) : les Événements restent en File
        et seront jugés au prochain Repos. L'échéance tardive de l'horloge est
        ignorée, la fenêtre étant refermée (``_window_open`` est faux).
        """
        self._window_open = False
        return [
            CancelIdleTimer(connection_id=EVENT_WINDOW_TIMER_ID),
            self._trace(
                "EventWindowCanceled",
                from_state=self._state,
                to_state=self._state,
                client_id=self._registered_client_id,
                extra={"events": len(self._event_queue)},
            ),
        ]

    async def _judge_batch(self) -> list[Command]:
        """Juge le lot hors Session (modèle **texte**), puis décide d'annoncer.

        Le verdict ne vient jamais du modèle Live : un modèle Live vocal répond
        toujours à un tour utilisateur, il ne sait pas « se taire ». Un modèle
        texte rend ``announce``/``message`` ; la Connexion Live n'est ouverte que
        pour faire parler l'Annonce. Un lot tu est simplement consommé : il
        n'entre pas au Journal (ADR-0007). Sans juge injecté, on se tait (faute
        fermée : dans le doute, ne pas déranger).
        """
        batch = self._event_queue
        self._event_queue = []
        if not batch:
            return self._decision("EventJudge")
        facts = [self._format_fact(event) for event in batch]
        verdict = (
            await self._judge.judge(facts)
            if self._judge is not None
            else Judgment(announce=False)
        )
        if not verdict.announce:
            return [
                self._trace(
                    "EventJudged",
                    from_state=SessionState.IDLE,
                    to_state=SessionState.IDLE,
                    client_id=self._registered_client_id,
                    extra={"events": len(batch), "announce": False},
                )
            ]
        return self._start_announcement(batch, verdict.message)

    def _start_announcement(
        self, batch: list[EventReceived], message: str
    ) -> list[Command]:
        """Ouvre l'unique Connexion Live et lui fait prononcer l'Annonce.

        Instruction d'annonce dédiée, **sans Rappel mémoire ni Tools** ; le
        message du jugement texte est transmis comme contenu de tour par une
        commande distincte de l'injection de mémoire. La Connexion est marquée
        ``auto`` : son premier son devient une Session automatique annoncée au
        Client de référence.
        """
        self._judged_batch = batch
        self._session_counter += 1
        self._connection_id = f"s{self._session_counter}"
        self._session_id = ""
        self._visible = False
        self._client_id = self._registered_client_id
        self._auto = True
        self._judging = True
        self._state = SessionState.LIVE_CONNECTING
        self._recent_utterances = []
        self._injected_memories = set()
        text = message.strip() or self._render_batch(batch)
        return [
            OpenLiveConnection(
                connection_id=self._connection_id,
                instruction=self._announce_prompt,
                tools=(),
            ),
            StartIdleTimer(
                connection_id=self._connection_id, timeout=ANNOUNCE_TIMEOUT
            ),
            SendAnnouncementToLlm(
                connection_id=self._connection_id,
                text=text,
            ),
            self._trace(
                "EventJudge",
                from_state=SessionState.IDLE,
                to_state=self._state,
                client_id=self._client_id,
                extra={"events": len(batch), "announce": True},
            ),
        ]

    @staticmethod
    def _format_fact(event: EventReceived) -> str:
        """Étiquette un fait par sa Source, forme ``[source] contenu``."""
        return f"[{event.source}] {event.content}"

    @classmethod
    def _render_batch(cls, batch: list[EventReceived]) -> str:
        """Un fait par ligne, étiqueté par Source (ordre d'arrivée, FIFO)."""
        lines = [f"- {cls._format_fact(event)}" for event in batch]
        return "\n".join([_EVENT_HEADER, *lines])

    async def _on_session_requested(
        self, event: SessionRequested
    ) -> list[Command]:
        commands: list[Command] = []
        original_state = self._state

        # « L'utilisateur gagne toujours » (ADR-0007) : un Wake word ou un tap
        # prime sur les automatismes d'Événement. Pendant une fenêtre ouverte il
        # l'annule en laissant la File intacte ; pendant un jugement en vol il
        # préempte la Connexion Live par le chemin de fin existant et remet les
        # faits du lot en tête de la File, pour un re-jugement ultérieur — sans
        # perte silencieuse.
        preempted: list[EventReceived] = []
        if self._window_open:
            commands.extend(self._cancel_window())
        if self._judging:
            # Le lot préempté est sorti **avant** la fin : il ne doit pas être
            # tracé comme perdu, il est remis en File juste après.
            preempted = self._judged_batch
            self._judged_batch = []
        if original_state is not SessionState.IDLE:
            commands.extend(self._teardown(SessionEndReason.REPLACED, "Replaced"))
        if preempted:
            # Les faits préemptés repassent devant tout ce qui a été mis en File
            # pendant le jugement, dans leur ordre d'origine ; le plafond de la
            # File reste tenu (les plus anciens — ici les préemptés — sont jetés
            # et tracés, comme à l'arrivée).
            self._event_queue = [*preempted, *self._event_queue]
            commands.extend(self._enforce_ceiling())

        self._session_counter += 1
        self._connection_id = f"s{self._session_counter}"
        self._session_id = ""
        self._visible = False
        self._client_id = event.client_id
        self._auto = event.auto
        self._state = SessionState.LIVE_CONNECTING
        self._recent_utterances = []
        self._injected_memories = set()

        # Activation (D3): assemble the generic prompt and the memory context the
        # Session opens with. The instruction is frozen here, at connect time.
        # Pinned profile facts are part of that instruction, so seed the recall
        # dedup with them: recall must not re-inject what is already in context.
        context = ""
        if self._memory is not None:
            context = await self._memory.session_context(self._user_id)
            self._injected_memories = set(
                await self._memory.pinned_facts(self._user_id)
            )
        instruction = (self._system_prompt + "\n\n" + context).strip()

        commands.append(
            OpenLiveConnection(
                connection_id=self._connection_id,
                instruction=instruction,
                tools=self._open_tools(),
            )
        )
        commands.append(
            StartIdleTimer(connection_id=self._connection_id, timeout=IDLE_TIMEOUT)
        )
        commands.append(
            self._trace(
                "SessionRequested",
                from_state=original_state,
                to_state=self._state,
                client_id=event.client_id,
            )
        )
        commands.append(
            Trace(
                source=_TRACE_SOURCE,
                kind="Activation",
                payload={
                    "connection_id": self._connection_id,
                    "user_id": self._user_id,
                    "has_context": bool(context),
                    "context_chars": len(context),
                },
            )
        )
        return commands

    def _open_tools(self) -> tuple[ToolSpec, ...]:
        if self._tools is None:
            return ()
        return self._tools.specs()

    def _on_client_audio(self, event: ClientAudio) -> list[Command]:
        if self._state in (SessionState.LIVE_ACTIVE, SessionState.SESSION_VISIBLE):
            # Raw mic bytes are not voice activity: never re-arm the idle timer.
            return [
                SendAudioToLlm(connection_id=self._connection_id, data=event.data),
                *self._decision("ClientAudio"),
            ]
        return self._decision("ClientAudio")

    def _on_client_disconnected(self, event: ClientDisconnected) -> list[Command]:
        # La disparition du Client actif termine la Session par le chemin
        # déterministe unique ; celle d'un Client non-actif ne la touche pas.
        if (
            self._client_id
            and event.client_id == self._client_id
            and self._state is not SessionState.IDLE
        ):
            return self._teardown(SessionEndReason.CLIENT_GONE, "ClientDisconnected")
        return self._decision("ClientDisconnected", client_id=event.client_id)

    def _on_client_interrupt(self, event: ClientInterrupt) -> list[Command]:
        client_id = event.client_id or self._client_id
        if self._state is SessionState.SESSION_VISIBLE:
            # Barge-in: the client flushes its own playback; the Session stays
            # visible. No command is emitted for the desktop to stop audio.
            return self._decision("barge_in", client_id=client_id)
        return self._decision("ClientInterrupt", client_id=client_id)

    def _on_client_tool_result(self, event: ClientToolResult) -> list[Command]:
        # Reserved seam: T1 has no Tool routing, so the result is trace-only.
        return self._decision("ClientToolResult")

    def _on_llm_opened(self, event: LlmOpened) -> list[Command]:
        from_state = self._state
        if self._state is SessionState.LIVE_CONNECTING:
            self._state = SessionState.LIVE_ACTIVE
        return [
            self._trace(
                "LlmOpened",
                from_state=from_state,
                to_state=self._state,
                client_id=self._client_id,
            )
        ]

    async def _on_llm_audio(self, event: LlmAudio) -> list[Command]:
        if self._state is SessionState.LIVE_ACTIVE:
            self._audio_buffer.append(event.data)
            return [
                self._rearm_timer(),
                *(await self._become_visible("LlmAudio")),
            ]
        if self._state is SessionState.SESSION_VISIBLE:
            return [
                self._rearm_timer(),
                SendAudioToClient(client_id=self._client_id, data=event.data),
                *self._decision("LlmAudio"),
            ]
        return self._decision("LlmAudio")

    async def _on_llm_transcription(
        self, event: LlmTranscription
    ) -> list[Command]:
        if event.role is Role.USER:
            return await self._on_user_utterance(event)
        if event.role is Role.ASSISTANT and self._state is SessionState.LIVE_ACTIVE:
            judging = self._judging
            commands = await self._become_visible("LlmTranscription")
            if judging:
                # La Connexion de jugement redevient une Session ordinaire : son
                # timer reprend le réglage d'inactivité de Session.
                commands.insert(0, self._rearm_timer())
            await self._journal_speech(event.text)
            return commands
        if self._state is not SessionState.IDLE:
            await self._journal_speech(event.text)
        return self._decision("LlmTranscription")

    async def _on_user_utterance(self, event: LlmTranscription) -> list[Command]:
        if self._state is SessionState.IDLE:
            return self._decision("LlmTranscription")
        # Le premier tour utilisateur referme le tour d'annonce : la parole
        # suivante d'Athena est une réponse ordinaire, journalisée ``assistant``.
        self._announcing = False
        commands: list[Command] = [self._rearm_timer()]
        if self._memory is not None:
            await self._journal("user", event.text)
            text, trace = await self._recall(event.text)
            commands.append(trace)
            if text is not None:
                commands.append(
                    SendMemoryToLlm(connection_id=self._connection_id, text=text)
                )
        commands.extend(self._decision("LlmTranscription"))
        self._recent_utterances.append(event.text)
        return commands

    async def _journal(self, role: str, text: str) -> None:
        if self._memory is not None:
            await self._memory.append_turn(self._user_id, role, text)

    async def _journal_speech(self, text: str) -> None:
        """Journalise une transcription de l'assistant.

        Pendant le tour d'annonce (Annonce proactive d'un Événement), la parole
        qui annonce entre au Journal en rôle ``event`` — comme le fait annoncé —
        pour que la Consolidation l'exclue entièrement (ADR-0007). Hors de ce
        tour, la parole est une réponse ordinaire (``assistant``).
        """
        role = RECENT_EVENT_ROLE if self._announcing else "assistant"
        await self._journal(role, text)

    async def _journal_judged_facts(self) -> None:
        """Écrit les faits du lot jugé, en rôle ``event``, à l'Annonce.

        Le lot n'est journalisé qu'au moment où la Connexion de jugement devient
        visible : un jugement tu ne doit rien écrire au Journal (ticket #42).
        """
        batch = self._judged_batch
        self._judged_batch = []
        for fact in batch:
            await self._journal(RECENT_EVENT_ROLE, self._format_fact(fact))

    async def _recall(self, utterance: str) -> tuple[str | None, Trace]:
        window = self._recent_utterances[-self._recall_window_turns :]
        query = " ".join([*window, utterance])
        hits = await self._memory.recall(self._user_id, query)

        lines: list[str] = []
        for hit in hits:
            text = hit.text
            if not text or text in self._injected_memories:
                continue
            if hit.distance > self._recall_max_distance:
                continue
            self._injected_memories.add(text)
            lines.append(f"- {text}")
            if len(lines) >= self._recall_k:
                break

        trace = Trace(
            source=_TRACE_SOURCE,
            kind="Rappel",
            payload={
                "connection_id": self._connection_id,
                "query": query,
                "candidates": len(hits),
                "injected": len(lines),
            },
        )
        if not lines:
            return None, trace
        return _MEMORY_HEADER + "\n" + "\n".join(lines), trace

    async def _on_llm_tool_call(self, event: LlmToolCall) -> list[Command]:
        """Exécute un Tool du noyau et renvoie son résultat au modèle.

        Un nom inconnu (ou l'absence de registre) produit un résultat d'erreur
        tracé : le round-trip ne lève jamais, la Session reste vivante.
        """
        commands: list[Command] = [
            self._trace(
                "ToolCall",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
                level="debug",
                extra={"tool_id": event.tool_id, "name": event.name},
            )
        ]
        if self._tools is None:
            result: Mapping[str, object] = {
                "status": "error",
                "message": f"unknown tool: {event.name}",
            }
        else:
            result = await self._tools.execute(event.name, event.arguments)
        commands.append(
            SendToolResultToLlm(
                connection_id=self._connection_id,
                tool_id=event.tool_id,
                name=event.name,
                result=result,
            )
        )
        commands.append(
            self._trace(
                "ToolResult",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
                level="debug",
                extra={
                    "tool_id": event.tool_id,
                    "name": event.name,
                    "status": str(result.get("status", "ok")),
                },
            )
        )
        return commands

    def _on_llm_closed(self, event: LlmClosed) -> list[Command]:
        if not self._owns(event.connection_id):
            return self._decision(
                "LlmClosed", stale_connection_id=event.connection_id
            )
        if self._state in (
            SessionState.LIVE_CONNECTING,
            SessionState.LIVE_ACTIVE,
            SessionState.SESSION_VISIBLE,
        ):
            return self._teardown(SessionEndReason.NORMAL, "LlmClosed")
        return self._decision("LlmClosed")

    def _on_llm_failed(self, event: LlmFailed) -> list[Command]:
        if not self._owns(event.connection_id):
            return self._decision(
                "LlmFailed",
                cause=event.cause.value,
                stale_connection_id=event.connection_id,
            )
        if self._state is not SessionState.IDLE:
            return self._teardown(
                SessionEndReason.ERROR, "LlmFailed", cause=event.cause.value
            )
        return self._decision("LlmFailed", cause=event.cause.value)

    def _on_shutdown_requested(self, event: ShutdownRequested) -> list[Command]:
        if self._state is not SessionState.IDLE:
            commands = self._teardown(SessionEndReason.SHUTDOWN, "Shutdown")
        else:
            commands = self._decision("ShutdownRequested")
        # La File d'attente d'Événements est éphémère : à l'arrêt, les faits non
        # jugés sont perdus et tracés un par un, jamais jugés (ADR-0007).
        for queued in self._event_queue:
            commands.append(self._event_drop_trace(queued, EventDropReason.SHUTDOWN))
        self._event_queue = []
        self._window_open = False
        return commands

    async def _on_timer_fired(self, event: TimerFired) -> list[Command]:
        if event.connection_id == EVENT_WINDOW_TIMER_ID:
            return await self._on_event_window_fired()
        if self._owns(event.connection_id) and self._state is not SessionState.IDLE:
            return self._teardown(SessionEndReason.TIMEOUT, "Timeout")
        return self._decision("TimerFired")

    async def _on_event_window_fired(self) -> list[Command]:
        """Échéance de la fenêtre : juge le lot entier, puis annonce si retenu.

        Si le noyau a quitté le Repos entre-temps (l'utilisateur a pris la
        parole), la fenêtre se referme sans juger et la File reste intacte :
        elle sera rouverte au prochain Repos.
        """
        if not self._window_open:
            return self._decision("EventWindowFired")
        self._window_open = False
        if self._state is not SessionState.IDLE:
            return self._decision("EventWindowFired")
        return await self._judge_batch()

    def _rearm_timer(self) -> StartIdleTimer:
        return StartIdleTimer(connection_id=self._connection_id, timeout=IDLE_TIMEOUT)

    def _owns(self, connection_id: str) -> bool:
        """Une identité vide n'est pas un joker : seul l'id courant compte."""
        return connection_id != "" and connection_id == self._connection_id

    async def _become_visible(self, kind: str) -> list[Command]:
        from_state = self._state
        announcing = self._judging
        self._state = SessionState.SESSION_VISIBLE
        self._visible = True
        self._judging = False
        self._session_id = self._connection_id
        if announcing:
            # Annonce proactive : le fait annoncé entre au Journal en rôle
            # ``event`` (le lot jugé n'est journalisé qu'ici, jamais s'il se
            # tait), et le tour d'annonce reste ouvert pour la parole d'annonce.
            self._announcing = True
            await self._journal_judged_facts()
        commands: list[Command] = [
            AnnounceSessionStarted(client_id=self._client_id, auto=self._auto)
        ]
        for buffered in self._audio_buffer:
            commands.append(
                SendAudioToClient(client_id=self._client_id, data=buffered)
            )
        self._audio_buffer.clear()
        commands.append(
            self._trace(
                kind,
                from_state=from_state,
                to_state=self._state,
                client_id=self._client_id,
            )
        )
        return commands

    def _teardown(
        self,
        reason: SessionEndReason,
        kind: str,
        *,
        cause: str | None = None,
    ) -> list[Command]:
        """Single deterministic termination path.

        Ordering is fixed: cancel the idle timer, close the Live connection,
        announce the Session end only when the Session had become visible,
        then trace both transitions Idle <- Closing <- origin.
        Afterwards the Coordinator is back to Idle with no residual identity,
        so no zombie Session can linger.
        """
        from_state = self._state
        old_connection_id = self._connection_id
        old_session_id = self._session_id
        old_client_id = self._client_id
        was_visible = self._visible
        self._state = SessionState.CLOSING
        commands: list[Command] = [
            CancelIdleTimer(connection_id=old_connection_id),
            CloseLiveConnection(connection_id=old_connection_id),
        ]
        if was_visible:
            commands.append(
                AnnounceSessionEnded(client_id=old_client_id, reason=reason)
            )
        commands.append(
            self._trace(
                kind,
                from_state=from_state,
                to_state=SessionState.CLOSING,
                client_id=old_client_id,
                reason=reason.name,
                cause=cause,
                connection_id=old_connection_id,
                session_id=old_session_id,
            )
        )
        commands.append(
            self._trace(
                "SessionClosed",
                from_state=SessionState.CLOSING,
                to_state=SessionState.IDLE,
                client_id=old_client_id,
                reason=reason.name,
                cause=cause,
                connection_id=old_connection_id,
                session_id=old_session_id,
            )
        )
        self._state = SessionState.IDLE
        # Un lot de jugement en vol n'a pas été Annoncé : sa perte est comptée
        # un fait par une trace, sans quoi elle serait silencieuse (#40/#41).
        # Le chemin d'annonce et la préemption l'ont déjà vidé.
        for fact in self._judged_batch:
            commands.append(
                self._event_drop_trace(fact, EventDropReason.JUDGMENT_ABORTED)
            )
        self._reset()
        return commands

    def _reset(self) -> None:
        self._client_id = ""
        self._connection_id = ""
        self._session_id = ""
        self._visible = False
        self._auto = False
        self._judging = False
        self._announcing = False
        self._judged_batch = []
        self._audio_buffer.clear()
        self._recent_utterances = []
        self._injected_memories = set()

    def _decision(
        self,
        kind: str,
        *,
        client_id: str | None = None,
        cause: str | None = None,
        stale_connection_id: str | None = None,
    ) -> list[Command]:
        """Trace for a decision that leaves the state unchanged."""
        return [
            self._trace(
                kind,
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id if client_id is None else client_id,
                cause=cause,
                level="debug",
                stale_connection_id=stale_connection_id,
            )
        ]

    def _trace(
        self,
        kind: str,
        *,
        from_state: SessionState,
        to_state: SessionState,
        client_id: str = "",
        reason: str | None = None,
        cause: str | None = None,
        connection_id: str | None = None,
        session_id: str | None = None,
        stale_connection_id: str | None = None,
        level: str = "info",
        extra: Mapping[str, object] | None = None,
    ) -> Trace:
        payload: dict[str, object] = {
            "connection_id": (
                self._connection_id if connection_id is None else connection_id
            ),
            "session_id": self._session_id if session_id is None else session_id,
            "client_id": client_id,
            "from_state": from_state.value,
            "to_state": to_state.value,
        }
        if reason is not None:
            payload["reason"] = reason
        if cause is not None:
            payload["cause"] = cause
        if extra is not None:
            payload.update(extra)
        if stale_connection_id is not None:
            payload["stale_connection_id"] = stale_connection_id
        return Trace(source=_TRACE_SOURCE, kind=kind, payload=payload, level=level)
