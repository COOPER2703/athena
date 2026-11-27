from __future__ import annotations

import re
from enum import StrEnum
from typing import Callable, Mapping, Sequence

from protocol import SessionEndReason

from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    CloseLiveConnection,
    Command,
    FailTour,
    FinishTour,
    OpenLiveConnection,
    RefuseSession,
    RefuseTour,
    SendAudioToClient,
    SendAudioToLlm,
    SendAnnouncementToLlm,
    SendAudioToTour,
    SendMemoryToLlm,
    SendTextToTour,
    SendToolCallToTour,
    SendToolResultToLlm,
    SendUserTextToLlm,
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
    TourCanceled,
    TourRequested,
)
from server.core.plugins import PluginRegistry
from server.core.ports import EventJudge, Judgment, MemoryStore, ToolSpec
from server.core.session_tools import END_SESSION
from server.core.state import SessionState
from server.core.tools import ToolRegistry
from server.core.untrusted import flatten_untrusted, sanitize_untrusted

_TRACE_SOURCE = "core"
IDLE_TIMEOUT = 30.0
# ``end_session`` ferme la Session **immédiatement** (ADR-0022) : plus de grâce,
# plus de plafond, plus d'état de clôture en attente. Le Client finit de jouer
# l'audio déjà reçu ; le noyau, lui, ne fait plus rien.


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
# Garde-fou d'un Tour texte (ADR-0024) : le Tour se referme de lui-même si le
# modèle ne rend rien, pour que le noyau ne reste jamais occupé par une réponse
# qui n'arrive pas. Un Tour qui répond se termine bien avant.
TOUR_TIMEOUT = 30.0
_MEMORY_HEADER = "[MÉMOIRE]"
_EVENT_HEADER = "[ÉVÉNEMENTS]"
# Un contenu externe ne doit jamais pouvoir forger l'en-tête du bloc de faits
# ni en ouvrir un second : ses occurrences sont neutralisées dans le rendu
# destiné au modèle (#81). Le Journal et le dédoublonnage du Rappel gardent,
# eux, la forme brute (``_format_fact``).
_EVENT_HEADER_TOKEN = re.compile(
    r"\[\s*(?:événements|evenements)\s*\]", re.IGNORECASE
)
_HEADER_REDACTED = "[en-tête retiré]"


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
        plugins: PluginRegistry | None = None,
        recall_k: int = 5,
        recall_window_turns: int = 3,
        recall_max_distance: float = 0.6,
        connected_clients: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._memory = memory
        self._user_id = user_id
        self._system_prompt = system_prompt
        self._announce_prompt = announce_prompt
        self._judge = judge
        # Le Plugin Registry possède le registre de Tools unique (noyau + plugins)
        # et les instructions de plugin ; ``tools`` reste le seam des tests qui
        # n'injectent qu'un registre nu (issue #28).
        self._plugins = plugins
        self._tools = plugins.tools if plugins is not None else tools
        self._recall_k = recall_k
        self._recall_window_turns = recall_window_turns
        self._recall_max_distance = recall_max_distance
        self._state = SessionState.IDLE
        # Présence des Clients (ADR-0028, ticket #193) : couture injectée, sans
        # import d'adaptateur. ``None`` signifie « présence inconnue » et
        # conserve l'ancien comportement (fenêtre ouverte sans auditeur) : c'est
        # un défaut de compatibilité pour les tests du noyau, jamais la règle de
        # production — ``build_app`` injecte **toujours** une vraie lecture.
        # Le callable rend les identifiants connectés, dans l'ordre de
        # préférence (le premier reçoit une Annonce si l'enregistré a disparu).
        self._connected_clients = connected_clients
        self._client_id = ""
        self._connection_id = ""
        self._session_id = ""
        self._visible = False
        self._auto = False
        self._judging = False
        self._session_counter = 0
        self._audio_buffer: list[bytes] = []
        # Tour texte en cours (ADR-0024) : un Tour n'est pas une Session, il ne
        # franchit jamais ``_become_visible`` et ne possède aucun Client. Le
        # compteur d'identité est distinct de celui des Sessions pour qu'une
        # ``LlmClosed`` tardive d'une Session ne referme pas un Tour.
        self._tour = False
        self._tour_id = ""
        self._tour_counter = 0
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

    def set_judge(self, judge: EventJudge | None) -> None:
        """Adopte un juge construit plus tard, à la fin de l'Installation.

        Sans clé Gemini, le noyau démarre sans juge ; la première clé active
        les surfaces d'exécution à chaud et le juge arrive ici, avant le premier
        Événement jugé. Aucune Session ne peut être en cours à cet instant.
        """
        self._judge = judge

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def owner_client_id(self) -> str:
        """Client propriétaire de la Session en cours, vide hors Session.

        Lecture seule : la Console web lit cette valeur à travers l'instantané
        de la racine de composition (ADR-0016). Le nom évite volontairement
        ``active_client_id``, réservé aux garde-fous des adaptateurs.

        Une Connexion Live n'est pas encore une Session : tant que la Session
        n'est pas visible, aucun Client n'est actif et la valeur reste vide
        (GLOSSARY, Connexion Live et Client actif).
        """
        return self._client_id if self._visible else ""

    @property
    def session_client_id(self) -> str:
        """Client qui a demandé la Session en cours, **dès** la Connexion Live.

        Contrairement à ``owner_client_id``, la valeur est renseignée avant que
        la Session ne devienne visible : la Console s'en sert pour adresser
        ``ready`` et les transcriptions à son Client Console (ADR-0027).
        """
        return self._client_id

    def busy_reason(self) -> str:
        """Code stable de l'occupation du noyau, vide au Repos.

        La Console s'en sert pour **refuser** plutôt que préempter : ``tour_busy``
        quand un Tour tient la Connexion Live, ``session_busy`` quand une Session
        (visible ou en cours d'ouverture) appartient déjà à un Client.
        """
        if self._tour:
            return "tour_busy"
        if self._state is not SessionState.IDLE:
            return "session_busy"
        return ""

    def _connected_client_ids(self) -> Sequence[str] | None:
        """Identifiants des Clients connectés, ou ``None`` si la présence est inconnue.

        ``None`` n'est rendu que par le défaut de test ; en production, la
        racine de composition injecte toujours une lecture réelle (transport
        plus Client Console éphémère).
        """
        if self._connected_clients is None:
            return None
        return self._connected_clients()

    def _has_connected_client(self) -> bool:
        """Vrai dès qu'un Client peut réellement recevoir une Annonce."""
        client_ids = self._connected_client_ids()
        return client_ids is None or bool(client_ids)

    def _announcement_target(self) -> str:
        """Client qui reçoit l'Annonce, vide si aucun Client n'est connecté.

        Le dernier Client enregistré ne fait pas foi : il peut être périmé
        après une déconnexion. On préfère l'enregistré s'il est **encore**
        connecté, sinon le premier Client connecté (ordre de préférence de la
        couture), sinon personne. Avec une présence inconnue (défaut de test),
        on garde l'ancien comportement : l'enregistré, même sans vérification.
        """
        client_ids = self._connected_client_ids()
        if client_ids is None:
            return self._registered_client_id
        for client_id in client_ids:
            if client_id == self._registered_client_id:
                return client_id
        return client_ids[0] if client_ids else ""

    def _defer_for_lack_of_client(self, count: int) -> list[Command]:
        """Trace un report faute de Client, sans consommer la File.

        Un seul nom de trace (``EventDeferred``) et une raison stable
        (``no_client``) pour que le Diagnostic explique un délai, que le report
        vienne de l'arrivée d'un Événement au Repos ou de l'échéance de la
        fenêtre.
        """
        return [
            self._trace(
                "EventDeferred",
                from_state=self._state,
                to_state=self._state,
                client_id=self._registered_client_id,
                reason="no_client",
                extra={"events": count},
            )
        ]

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
        if isinstance(event, TourRequested):
            return await self._on_tour_requested(event)
        if isinstance(event, TourCanceled):
            return self._on_tour_canceled(event)
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
        # Au Repos sans Client, l'Événement patiente : la fenêtre ne s'ouvrira
        # pas (``_open_window_if_needed``), et le report est tracé pour que le
        # Diagnostic explique le délai (ADR-0028, story 25).
        if self._state is SessionState.IDLE and not self._has_connected_client():
            commands.extend(
                self._defer_for_lack_of_client(len(self._event_queue))
            )
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

        Sans Client connecté, elle ne s'ouvre pas : les Événements patientent
        dans l'ordre d'arrivée (ADR-0028). Comme ``handle`` l'appelle après
        **chaque** Événement, ``ClientRegistered`` rouvre la fenêtre sans
        attendre un nouvel Événement.
        """
        if (
            self._state is not SessionState.IDLE
            or self._window_open
            or not self._event_queue
            or not self._has_connected_client()
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
        facts = [self._safe_fact(event) for event in batch]
        verdict = (
            await self._judge.judge(facts)
            if self._judge is not None
            else Judgment(announce=False, failed=True)
        )
        if verdict.failed:
            # Verdict manquant (panne du juge, réponse malformée) : faute fermée,
            # mais tracée **distinctement** d'un fait jugé banal, pour qu'une
            # panne silencieuse (quota, réseau) ne se lise pas comme « tout est
            # banal » (ADR-0012).
            return [
                self._trace(
                    "EventJudgmentFailed",
                    from_state=SessionState.IDLE,
                    to_state=SessionState.IDLE,
                    client_id=self._registered_client_id,
                    extra={"events": len(batch)},
                    level="error",
                )
            ]
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
        message = verdict.message.strip()
        if not message:
            # « Annonce » sans phrase : le verdict est malformé. Le lot ne doit
            # jamais devenir le tour à prononcer — ce serait lire à voix haute
            # le contenu externe, instructions comprises (#81). Faute fermée.
            return [
                self._trace(
                    "EventJudgmentFailed",
                    from_state=SessionState.IDLE,
                    to_state=SessionState.IDLE,
                    client_id=self._registered_client_id,
                    reason="missing_message",
                    extra={"events": len(batch)},
                    level="error",
                )
            ]
        return await self._start_announcement(batch, message)

    async def _start_announcement(
        self, batch: list[EventReceived], message: str
    ) -> list[Command]:
        """Ouvre l'unique Connexion Live et lui fait prononcer l'Annonce.

        La Session automatique reprend l'Activation d'une Session ordinaire
        (prompt système, instructions de plugin, contexte mémoire — ADR-0013),
        et l'instruction d'Annonce s'y ajoute pour qu'Athena sache qu'un
        Événement a ouvert la Session. Elle s'en distingue sur un point, qui
        est **structurel** : la Connexion est ouverte sans aucun outil, pour
        qu'un Événement piégé ne puisse jamais déclencher une action (#81 ;
        ADR-0020). Le message du juge est la phrase à
        prononcer ; les faits assainis du lot sont transmis au Live comme
        contexte (``facts``), sans être prononcés, pour qu'Athena puisse
        détailler l'Événement à la demande. La Connexion est marquée ``auto`` :
        son premier son devient une Session automatique annoncée au Client de
        référence.

        La cible est un Client **réellement connecté** (ADR-0028) : si le
        dernier enregistré a disparu, le premier connecté reçoit l'Annonce.
        Quand la présence est connue et qu'aucun Client n'est connecté au
        moment d'ouvrir, la Connexion n'est pas ouverte : le lot retourne en
        tête de File, intact, et le report est tracé. Une présence inconnue
        (défaut de test) garde l'ancien comportement.
        """
        target = self._announcement_target()
        if not target and self._connected_clients is not None:
            self._event_queue = [*batch, *self._event_queue]
            return self._defer_for_lack_of_client(len(batch))
        self._judged_batch = batch
        self._session_counter += 1
        self._connection_id = f"s{self._session_counter}"
        self._session_id = ""
        self._visible = False
        self._client_id = target
        self._auto = True
        self._judging = True
        self._state = SessionState.LIVE_CONNECTING
        self._recent_utterances = []

        # Activation, comme une Session ordinaire : le prompt système, les
        # instructions de plugin et le contexte mémoire (profil épinglé, tours
        # récents) font partie de l'instruction, l'instruction d'Annonce s'y
        # intercale. Les faits bruts du lot y étant aussi, semer le
        # dédoublonnage du Rappel avec eux évite de les réinjecter au premier
        # tour utilisateur.
        context = ""
        injected: set[str] = set()
        if self._memory is not None:
            context = await self._memory.session_context(self._user_id)
            injected = set(await self._memory.pinned_facts(self._user_id))
        self._injected_memories = injected | {
            self._format_fact(event) for event in batch
        }
        instruction = self._compose_instruction(
            self._system_prompt,
            self._plugin_instruction(),
            self._announce_prompt,
            context,
        )

        return [
            *self._open_live(
                instruction=instruction,
                tools=(),
                timeout=ANNOUNCE_TIMEOUT,
            ),
            SendAnnouncementToLlm(
                connection_id=self._connection_id,
                # Le message du juge est la seule phrase prononcée ; les faits
                # ne sont que le contexte séparé du suivi. ``_judge_batch``
                # garantit qu'un verdict d'annonce porte toujours une phrase,
                # donc le contenu externe n'est jamais lu à voix haute (#81).
                text=message,
                facts=self._render_batch(batch),
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

    @staticmethod
    def _safe_fact(event: EventReceived) -> str:
        """Fait rendu pour le modèle : une ligne, sans en-tête forgeable.

        ``_format_fact`` reste la forme brute du Journal et du dédoublonnage du
        Rappel. Ici, le contenu externe et son étiquette de Source sont aplatis
        (**un fait = une ligne**) et les occurrences de l'en-tête
        ``[ÉVÉNEMENTS]`` sont retirées : un contenu ne peut ni fabriquer un
        second fait, ni simuler un nouveau bloc, ni fermer l'étiquette de Source
        (#81). La règle d'aplatissement et de neutralisation est partagée avec
        le juge (``server.core.untrusted``).
        """
        source = flatten_untrusted(event.source).replace("]", ")")
        content = sanitize_untrusted(
            event.content,
            forbidden=_EVENT_HEADER_TOKEN,
            replacement=_HEADER_REDACTED,
        )
        return f"[{source}] {content}"

    @classmethod
    def _render_batch(cls, batch: list[EventReceived]) -> str:
        """Un fait par ligne, étiqueté par Source (ordre d'arrivée, FIFO)."""
        lines = [f"- {cls._safe_fact(event)}" for event in batch]
        return "\n".join([_EVENT_HEADER, *lines])

    async def _on_tour_requested(
        self, event: TourRequested
    ) -> list[Command]:
        """Ouvre un **Tour texte** : une Connexion Live, une réponse, au Repos.

        Le Tour réutilise exactement l'Activation d'une Session — prompt système,
        instructions de plugin, contexte mémoire, Tools du moment — mais il ne
        crée aucune Session : ``_visible`` reste faux, ``owner_client_id`` reste
        vide, et le retour au Repos passe par ``_end_tour``, jamais par
        ``_teardown``. Un Tour demandé alors que le noyau est occupé est refusé
        avec un motif lisible, sans mise en file (ADR-0024).
        """
        if self._state is not SessionState.IDLE:
            return [
                RefuseTour(tour_id=event.tour_id, reason="busy"),
                self._trace(
                    "TourRefused",
                    from_state=self._state,
                    to_state=self._state,
                    reason="busy",
                    extra={"tour_id": event.tour_id},
                ),
            ]

        commands: list[Command] = []
        # Un Tour prime sur la fenêtre d'Événements comme une Session : la
        # fenêtre se referme, la File reste intacte pour le prochain Repos.
        if self._window_open:
            commands.extend(self._cancel_window())

        self._tour_counter += 1
        self._connection_id = f"t{self._tour_counter}"
        self._tour_id = event.tour_id
        self._tour = True
        self._session_id = ""
        self._visible = False
        self._client_id = ""
        self._auto = False
        self._judging = False
        self._announcing = False
        self._state = SessionState.LIVE_CONNECTING
        self._recent_utterances = []
        self._injected_memories = set()

        context = ""
        if self._memory is not None:
            context = await self._memory.session_context(self._user_id)
            self._injected_memories = set(
                await self._memory.pinned_facts(self._user_id)
            )
            # L'entrée entre au Journal **à l'envoi** : un Tour texte n'a pas de
            # transcription d'entrée, il n'y a pas d'autre moment pour l'écrire.
            await self._journal("user", event.text, origin="text")

        instruction = self._compose_instruction(
            self._system_prompt, self._plugin_instruction(), context
        )
        commands.extend(
            self._open_live(
                instruction=instruction,
                tools=self._open_tools(),
                timeout=TOUR_TIMEOUT,
            )
        )
        if self._memory is not None:
            memory, trace = await self._recall(event.text)
            commands.append(trace)
            if memory is not None:
                commands.append(
                    SendMemoryToLlm(
                        connection_id=self._connection_id, text=memory
                    )
                )
        commands.append(
            SendUserTextToLlm(
                connection_id=self._connection_id, text=event.text
            )
        )
        commands.append(
            self._trace(
                "TourRequested",
                from_state=SessionState.IDLE,
                to_state=self._state,
                extra={
                    "tour_id": event.tour_id,
                    "has_context": bool(context),
                },
            )
        )
        return commands

    def _on_tour_canceled(self, event: TourCanceled) -> list[Command]:
        """La surface a fermé son flux : le Tour s'arrête, sans réponse écrite."""
        if not self._owns_tour(event.tour_id):
            return [
                self._trace(
                    "TourCanceled",
                    from_state=self._state,
                    to_state=self._state,
                    level="debug",
                    stale_tour_id=event.tour_id,
                )
            ]
        return self._end_tour("canceled", kind="TourCanceled")

    def _owns_tour(self, tour_id: str) -> bool:
        """Une identité de Tour vide n'est pas un joker : seul l'id courant compte."""
        return self._tour and tour_id != "" and tour_id == self._tour_id

    def _end_tour(
        self,
        reason: str,
        *,
        kind: str = "TourFinished",
        cause: str | None = None,
        failure: str | None = None,
    ) -> list[Command]:
        """Chemin unique de fin d'un Tour : fermeture, traces, retour au Repos.

        Distinct de ``_teardown`` (Session) : aucun ``AnnounceSessionEnded``,
        aucun ``SessionClosed``, et la destination reçoit un ``FinishTour`` ou un
        ``FailTour`` pour clore son flux ``text/event-stream``.
        """
        from_state = self._state
        old_connection_id = self._connection_id
        tour_id = self._tour_id
        self._state = SessionState.CLOSING
        commands: list[Command] = [
            CancelIdleTimer(connection_id=old_connection_id),
            CloseLiveConnection(connection_id=old_connection_id),
            self._trace(
                kind,
                from_state=from_state,
                to_state=SessionState.CLOSING,
                reason=reason,
                cause=cause,
                connection_id=old_connection_id,
                session_id="",
                extra={"tour_id": tour_id},
            ),
            self._trace(
                "TourClosed",
                from_state=SessionState.CLOSING,
                to_state=SessionState.IDLE,
                reason=reason,
                cause=cause,
                connection_id=old_connection_id,
                session_id="",
                extra={"tour_id": tour_id},
            ),
        ]
        self._state = SessionState.IDLE
        self._reset()
        if failure is not None:
            commands.append(FailTour(tour_id=tour_id, code=failure))
        else:
            commands.append(FinishTour(tour_id=tour_id, reason=reason))
        return commands

    async def _on_session_requested(
        self, event: SessionRequested
    ) -> list[Command]:
        commands: list[Command] = []
        original_state = self._state

        # Une demande non préemptante (Console, ADR-0027) est refusée dès que le
        # noyau est occupé, quel que soit l'occupant : la Console ne vole jamais
        # la Session d'un autre Client. La course avec la pré-vérification de la
        # racine de composition se ferme ici, avant tout teardown.
        if event.no_preempt:
            reason = self.busy_reason()
            if reason:
                return [
                    RefuseSession(client_id=event.client_id, reason=reason),
                    self._trace(
                        "SessionRefused",
                        from_state=original_state,
                        to_state=original_state,
                        client_id=event.client_id,
                        reason=reason,
                    ),
                ]

        # Un seul Tour à la fois (ADR-0024) : une Session demandée pendant un
        # Tour est refusée, jamais mise en file ni préemptée. Le refus est rendu
        # au Client par le message ``Error`` du Protocole, sans le changer.
        if self._tour:
            return [
                RefuseSession(client_id=event.client_id, reason="busy"),
                self._trace(
                    "SessionRefused",
                    from_state=original_state,
                    to_state=original_state,
                    client_id=event.client_id,
                    reason="busy",
                    extra={"tour_id": self._tour_id},
                ),
            ]

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

        # Activation (D3): assemble the generic prompt, the plugin instructions
        # and the memory context the Session opens with. The instruction is
        # frozen here, at connect time. Pinned profile facts are part of that
        # instruction, so seed the recall dedup with them: recall must not
        # re-inject what is already in context.
        context = ""
        if self._memory is not None:
            context = await self._memory.session_context(self._user_id)
            self._injected_memories = set(
                await self._memory.pinned_facts(self._user_id)
            )
        plugin_instruction = self._plugin_instruction()
        instruction = self._compose_instruction(
            self._system_prompt, plugin_instruction, context
        )

        commands.extend(
            self._open_live(
                instruction=instruction,
                tools=self._open_tools(),
                timeout=IDLE_TIMEOUT,
            )
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
                    "has_plugin_instruction": bool(plugin_instruction),
                    "plugin_instruction_chars": len(plugin_instruction),
                },
            )
        )
        return commands

    def _open_live(
        self,
        *,
        instruction: str,
        tools: tuple[ToolSpec, ...],
        timeout: float,
    ) -> list[Command]:
        """Les deux Commandes qui ouvrent une Connexion Live et arment son filet.

        Le rituel est commun à la Session, à l'Annonce et au Tour : seul
        l'instruction, la liste de Tools et le délai changent. L'Annonce garde
        ``tools=()`` — un contenu externe ne doit jamais déclencher une action
        (#81) —, décision provisoire d'ADR-0020.
        """
        return [
            OpenLiveConnection(
                connection_id=self._connection_id,
                instruction=instruction,
                tools=tools,
            ),
            StartIdleTimer(
                connection_id=self._connection_id, timeout=timeout
            ),
        ]

    def _open_tools(self) -> tuple[ToolSpec, ...]:
        if self._tools is None:
            return ()
        return self._tools.specs()

    def _plugin_instruction(self) -> str:
        """Instruction d'usage des plugins activés, vide sans plugin.

        Le registre la compose déjà dans un ordre déterministe (nom du plugin) :
        le noyau ne fait que l'insérer dans le prompt, il ne la réécrit pas.
        """
        if self._plugins is None:
            return ""
        return self._plugins.instruction()

    @staticmethod
    def _compose_instruction(*sections: str) -> str:
        """Assemble l'instruction d'une Connexion Live, sections vides omises.

        Ordre fixé par l'appelant : prompt système → instructions de plugin →
        instruction d'Annonce → contexte mémoire. Les sections non vides sont
        séparées par une ligne vide ; sans aucune section, l'instruction est la
        chaîne vide (jamais des sauts de ligne).
        """
        return "\n\n".join(section for section in sections if section).strip()

    def _in_live_session(self) -> bool:
        """Vrai pendant une Session Live active ou visible (jamais au Repos)."""
        return self._state in (
            SessionState.LIVE_ACTIVE,
            SessionState.SESSION_VISIBLE,
        )

    def _on_client_audio(self, event: ClientAudio) -> list[Command]:
        if self._in_live_session():
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
        # Un barge-in ne peut plus annuler de clôture : ``end_session`` ferme la
        # Session immédiatement (ADR-0022). Il est traité comme toujours, sans
        # aucun teardown.
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
        if self._tour:
            # Un Tour ne devient jamais visible : l'audio part vers sa
            # destination, jamais vers un Client (il n'y en a pas).
            if self._state is SessionState.LIVE_ACTIVE:
                return [
                    self._rearm_timer(),
                    SendAudioToTour(tour_id=self._tour_id, data=event.data),
                    *self._decision("LlmAudio"),
                ]
            return self._decision("LlmAudio")
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
        if self._tour:
            return await self._on_tour_transcription(event)
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
            await self._journal("user", event.text, origin="voice")
            text, trace = await self._recall(event.text)
            commands.append(trace)
            if text is not None:
                commands.append(
                    SendMemoryToLlm(connection_id=self._connection_id, text=text)
                )
        commands.extend(self._decision("LlmTranscription"))
        self._recent_utterances.append(event.text)
        return commands

    async def _on_tour_transcription(
        self, event: LlmTranscription
    ) -> list[Command]:
        """Transcription pendant un Tour : la réponse est écrite, puis le Tour finit.

        L'entrée du Tour est déjà au Journal depuis son envoi : une transcription
        d'entrée du modèle ne doit jamais produire un second tour utilisateur. La
        réponse, elle, est journalisée ``assistant`` avec l'origine ``text`` et
        poussée vers la destination ; le Tour se referme aussitôt, ce qui rend le
        noyau au Repos et libère la voix.
        """
        if event.role is Role.USER or not event.text.strip():
            return self._decision("LlmTranscription")
        await self._journal("assistant", event.text, origin="text")
        return [
            SendTextToTour(tour_id=self._tour_id, text=event.text),
            *self._end_tour("completed"),
        ]

    async def _journal(self, role: str, text: str, origin: str = "") -> None:
        if self._memory is not None:
            await self._memory.append_turn(
                self._user_id, role, text, origin=origin
            )

    async def _journal_speech(self, text: str) -> None:
        """Journalise une transcription de l'assistant.

        Pendant le tour d'annonce (Annonce proactive d'un Événement), la parole
        qui annonce entre au Journal en rôle ``event`` — comme le fait annoncé —
        pour que la Consolidation l'exclue entièrement (ADR-0007). Hors de ce
        tour, la parole est une réponse ordinaire (``assistant``).
        """
        role = RECENT_EVENT_ROLE if self._announcing else "assistant"
        await self._journal(role, text, origin="event" if self._announcing else "voice")

    async def _journal_judged_facts(self) -> None:
        """Écrit les faits du lot jugé, en rôle ``event``, à l'Annonce.

        Le lot n'est journalisé qu'au moment où la Connexion de jugement devient
        visible : un jugement tu ne doit rien écrire au Journal (ticket #42).
        """
        batch = self._judged_batch
        self._judged_batch = []
        for fact in batch:
            await self._journal(RECENT_EVENT_ROLE, self._format_fact(fact), origin="event")

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
        """Exécute un Tool (noyau ou plugin) et renvoie son résultat au modèle.

        Un nom inconnu (ou l'absence de registre) produit un résultat d'erreur
        tracé : le round-trip ne lève jamais, la Session reste vivante. Chaque
        trace porte le plugin propriétaire (``core`` pour un Tool du noyau,
        ``null`` pour un nom inconnu), pour attribuer appels et erreurs dans le
        flux de trace (ADR-0014).

        ``end_session`` relève de la politique du noyau (#130, ADR-0022) : il
        n'est accepté que dans une Session **ordinaire** active et la ferme
        **immédiatement** par le chemin de fin unique, avec ``COMPLETED``. Une
        Connexion de jugement ou une Session d'Annonce n'est pas ordinaire
        (``tools=()``, ADR-0020) : le modèle reçoit une erreur et la Session
        reste vivante.
        """
        plugin = self._tool_owner(event.name)
        is_end_session = event.name == END_SESSION
        # Le Tool de clôture n'a de sens que dans une Session ordinaire active :
        # ni la Connexion de jugement (``_judging``), ni la Session automatique
        # d'Annonce (``_auto``), ni un Tour texte (``_tour``) ne peuvent
        # l'appeler (#130, ADR-0020, ADR-0024).
        ordinary = (
            self._in_live_session()
            and not self._judging
            and not self._auto
            and not self._tour
        )
        commands: list[Command] = [
            self._trace(
                "ToolCall",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
                level="debug",
                extra={
                    "tool_id": event.tool_id,
                    "name": event.name,
                    "plugin": plugin,
                },
            )
        ]
        if is_end_session and not ordinary:
            result: Mapping[str, object] = {
                "status": "error",
                "message": "end_session outside an ordinary active session",
            }
        elif self._tools is None:
            result = {
                "status": "error",
                "message": f"unknown tool: {event.name}",
            }
        else:
            result = await self._tools.execute(event.name, event.arguments)
        if self._tour:
            # La surface voit le nom, les arguments et l'issue — jamais le corps
            # du résultat, qui peut porter des données de plugin.
            commands.append(
                SendToolCallToTour(
                    tour_id=self._tour_id,
                    name=event.name,
                    arguments=event.arguments,
                    status=str(result.get("status", "ok")),
                )
            )
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
                    "plugin": plugin,
                    "status": str(result.get("status", "ok")),
                },
            )
        )
        # ``end_session`` accepté : la Session se ferme immédiatement (#130,
        # ADR-0022). Le résultat du Tool est tout de même émis avant le
        # teardown, puis le Client reçoit ``SessionEnded`` même si la Session
        # n'était pas encore devenue visible : un Client qui a démarré une
        # Session doit toujours revenir à ``LISTENING``.
        if is_end_session and ordinary and result.get("status") == "ok":
            commands.extend(
                self._teardown(
                    SessionEndReason.COMPLETED,
                    "EndSession",
                    force_announce=True,
                )
            )
        return commands

    def _tool_owner(self, name: str) -> str | None:
        """Plugin propriétaire d'un Tool, ``None`` quand le nom est inconnu."""
        if self._tools is None:
            return None
        return self._tools.owner_of(name)

    def _on_llm_closed(self, event: LlmClosed) -> list[Command]:
        if not self._owns(event.connection_id):
            return self._decision(
                "LlmClosed", stale_connection_id=event.connection_id
            )
        if self._tour:
            return self._end_tour("closed")
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
        if self._tour and self._state is not SessionState.IDLE:
            return self._end_tour(
                "failed", cause=event.cause.value, failure="llm_failed"
            )
        if self._state is not SessionState.IDLE:
            return self._teardown(
                SessionEndReason.ERROR, "LlmFailed", cause=event.cause.value
            )
        return self._decision("LlmFailed", cause=event.cause.value)

    def _on_shutdown_requested(self, event: ShutdownRequested) -> list[Command]:
        if self._tour:
            # Un Tour en vol se referme avant l'arrêt : la surface reçoit sa fin
            # au lieu d'une connexion coupée sans explication.
            commands = self._end_tour(
                "canceled", kind="TourCanceled", cause="shutdown"
            )
        elif self._state is not SessionState.IDLE:
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
            if self._tour:
                return self._end_tour("timeout")
            return self._teardown(SessionEndReason.TIMEOUT, "Timeout")
        return self._decision("TimerFired")

    async def _on_event_window_fired(self) -> list[Command]:
        """Échéance de la fenêtre : juge le lot entier, puis annonce si retenu.

        Si le noyau a quitté le Repos entre-temps (l'utilisateur a pris la
        parole), la fenêtre se referme sans juger et la File reste intacte :
        elle sera rouverte au prochain Repos.

        Si le dernier Client a disparu pendant que le minuteur courait, même
        chose : la fenêtre se referme sans juger, la File reste intacte, et le
        report est tracé. C'est le prochain ``ClientRegistered`` qui rouvrira
        la fenêtre — aucun minuteur à annuler, donc aucune identité de timer
        perdue (ADR-0028).
        """
        if not self._window_open:
            return self._decision("EventWindowFired")
        self._window_open = False
        if self._state is not SessionState.IDLE:
            return self._decision("EventWindowFired")
        if not self._has_connected_client():
            return self._defer_for_lack_of_client(len(self._event_queue))
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
        force_announce: bool = False,
    ) -> list[Command]:
        """Single deterministic termination path.

        Ordering is fixed: cancel the idle timer, close the Live connection,
        announce the Session end only when the Session had become visible —
        or when ``force_announce`` is set, pour qu'un Client qui a démarré une
        Session jamais devenue visible revienne tout de même à ``LISTENING``
        (``end_session``, ADR-0022) — then trace both transitions
        Idle <- Closing <- origin. Afterwards the Coordinator is back to Idle
        with no residual identity, so no zombie Session can linger.
        """
        from_state = self._state
        old_connection_id = self._connection_id
        old_session_id = self._session_id
        old_client_id = self._client_id
        was_visible = self._visible
        self._state = SessionState.CLOSING
        commands: list[Command] = [
            CancelIdleTimer(connection_id=old_connection_id),
        ]
        commands.append(CloseLiveConnection(connection_id=old_connection_id))
        if was_visible or force_announce:
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
        self._tour = False
        self._tour_id = ""
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
        stale_tour_id: str | None = None,
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
        if stale_tour_id is not None:
            payload["stale_tour_id"] = stale_tour_id
        return Trace(source=_TRACE_SOURCE, kind=kind, payload=payload, level=level)
