from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, NamedTuple, Protocol, Sequence

from plugin_api import ToolSpec
from protocol import SessionEndReason

# ``ToolSpec`` appartient au contrat public (``plugin_api``) ; il est ré-exporté
# ici pour que les imports existants du noyau continuent de marcher.


class MissingSecretError(RuntimeError):
    """Levée quand un secret requis est introuvable dans sa source."""


class SecretResolver(Protocol):
    """Port de résolution des secrets.

    Les adaptateurs reçoivent la valeur déjà résolue et ne lisent jamais
    l'environnement eux-mêmes (ADR-0001). Le port vit dans le noyau pour qu'un
    adaptateur n'ait pas à importer la racine de composition.
    """

    def resolve(self, name: str) -> str: ...


class ClientLink(Protocol):
    async def send_audio(self, client_id: str, data: bytes) -> None: ...

    async def announce_session_started(self, client_id: str, auto: bool) -> None: ...

    async def announce_session_ended(
        self, client_id: str, reason: SessionEndReason
    ) -> None: ...

    async def send_tool_call(
        self, client_id: str, tool_id: str, name: str, arguments_json: bytes
    ) -> None: ...


class LlmSocket(Protocol):
    async def open(
        self,
        session_id: str,
        instruction: str = "",
        tools: Sequence[ToolSpec] = (),
    ) -> None: ...

    async def close(self, session_id: str) -> None: ...

    async def send_audio(self, session_id: str, data: bytes) -> None: ...

    async def send_memory(self, session_id: str, text: str) -> None: ...

    async def send_announcement(
        self, session_id: str, text: str, facts: str = ""
    ) -> None: ...

    async def send_tool_result(
        self,
        session_id: str,
        tool_id: str,
        name: str,
        result: Mapping[str, object],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Judgment:
    """Verdict d'un jugement d'Événements (texte, non-live).

    ``announce`` décide si le lot mérite une Annonce proactive ; ``message`` est
    la phrase à prononcer, vide quand ``announce`` est faux.

    ``failed`` distingue un verdict **rendu** (le juge a bien décidé, y compris
    « se taire ») d'un verdict **manquant** (panne réseau, réponse malformée).
    Les deux donnent ``announce=False`` — c'est la faute fermée — mais un échec
    ne doit jamais être confondu avec un fait jugé banal dans le trail : il est
    tracé comme tel (ADR-0012).
    """

    announce: bool
    message: str = ""
    failed: bool = False


class EventJudge(Protocol):
    """Juge un lot d'Événements hors Session (ADR-0007, amendé).

    Le verdict vient d'un modèle **texte**, jamais du modèle Live vocal : un
    modèle Live répond toujours à un tour utilisateur, il ne sait pas « se
    taire ». L'adaptateur possède l'appel réseau et ne lève jamais : un échec de
    jugement renvoie ``Judgment(announce=False, failed=True)`` (faute fermée :
    dans le doute, on ne dérange pas l'utilisateur). ``failed`` doit refléter un
    verdict manquant, jamais un verdict rendu.
    """

    async def judge(self, facts: Sequence[str]) -> Judgment: ...


class Clock(Protocol):
    def schedule(self, connection_id: str, timeout: float) -> None: ...

    def cancel(self, connection_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A candidate recalled from memory (pure; the kernel applies the policy)."""

    text: str
    distance: float
    source: str


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One Journal turn, as read back by the Consolidation (pure)."""

    id: int
    role: str
    text: str


class MemoryStore(Protocol):
    """Async memory port injected into the pure kernel (D2).

    ``session_context``/``append_turn`` are the ticket-#26 seam; ``recall``,
    ``save`` and ``forget`` are declared here so the adapter can grow into them
    without the kernel changing signature (issues #27/#29). The Consolidation
    seam (#30) is ``journal_entries_since``/``last_consolidation``/
    ``mark_consolidated`` (Journal window and run marker), ``is_duplicate`` and
    ``append_fact`` (long-term writes), and ``purge_older_than`` (retention).
    """

    async def session_context(self, user_id: str) -> str: ...

    async def pinned_facts(self, user_id: str) -> list[str]: ...

    async def append_turn(self, user_id: str, role: str, text: str) -> None: ...

    async def recall(self, user_id: str, query: str) -> list[MemoryHit]: ...

    async def save(self, user_id: str, content: str, *, pinned: bool) -> None: ...

    async def forget(
        self, user_id: str, query: str, max_distance: float
    ) -> bool: ...

    async def journal_entries_since(
        self, user_id: str, since: datetime | None
    ) -> list[JournalEntry]: ...

    async def last_consolidation(self, user_id: str) -> datetime | None: ...

    async def mark_consolidated(self, user_id: str, when: datetime) -> None: ...

    async def is_duplicate(
        self, user_id: str, text: str, max_distance: float
    ) -> bool: ...

    async def append_fact(
        self,
        user_id: str,
        text: str,
        *,
        pinned: bool = False,
        source_ids: Sequence[int] = (),
    ) -> None: ...

    async def purge_older_than(self, days: int | None = None) -> int: ...


class FactExtractor(Protocol):
    """Turns a Journal transcript into durable facts (issue #30).

    The adapter owns the LLM call (non-live ``generate_content``) and never
    raises: an extraction failure yields an empty list.
    """

    async def extract(self, transcript: str) -> list[str]: ...


class Embedder(Protocol):
    """Turns text into a vector, off the event loop (adapter owns ``to_thread``)."""

    async def embed(self, text: str) -> list[float]: ...


class TraceEntry(NamedTuple):
    timestamp: float
    source: str
    kind: str
    payload: Mapping[str, object]
    level: str = "info"


class TraceSink(Protocol):
    def emit(self, entry: TraceEntry) -> None: ...
