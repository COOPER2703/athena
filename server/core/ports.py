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


class AuthError(Exception):
    """Échec d'une opération d'authentification, porteur d'un code stable.

    Le code appartient au vocabulaire du port (``invalid_code``,
    ``already_installed``, ``invalid_credentials``, ``too_many_attempts``,
    ``weak_password``, ``invalid_username``) : les surfaces le traduisent en
    statut HTTP, le port ne connaît pas HTTP. ``retry_after`` accompagne
    ``too_many_attempts`` avec le nombre de secondes restantes.
    """

    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class Account:
    """Le Compte unique de l'admin : identité d'accès, distincte de l'Utilisateur."""

    id: str
    username: str


@dataclass(frozen=True, slots=True)
class IssuedTicket:
    """Ticket Console fraîchement ouvert, avec son secret en clair (une fois).

    Le secret n'est jamais réaffiché ni stocké : la surface le dépose dans le
    cookie ``HttpOnly`` puis l'oublie.
    """

    account: Account
    ticket_id: str
    secret: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class AccessToken:
    """Un Jeton d'accès tel que la Console le montre.

    Le secret et son empreinte n'y figurent **jamais** : seul le préfixe
    affichable reste, pour qu'un humain reconnaisse le Jeton sans pouvoir s'en
    servir. ``source`` n'est renseignée que pour la nature ``source`` (Webhook) ;
    ``revoked_at`` porte la révocation, qui reste visible après coup.
    """

    id: str
    name: str
    kind: str
    source: str | None
    prefix: str
    created_at: float
    last_used_at: float | None = None
    revoked_at: float | None = None


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    """Jeton fraîchement créé, avec son secret en clair (une seule fois).

    Le secret n'est ni relisible ni stocké : la surface l'affiche une fois puis
    l'oublie. ``token`` est la ligne durable, sans secret.
    """

    token: AccessToken
    secret: str


@dataclass(frozen=True, slots=True)
class AuthenticatedTicket:
    """Ticket Console valide, résolu en Compte.

    ``refreshed`` signale que l'expiration glissante a été repoussée pendant
    cette vérification : la surface peut alors reposer le cookie. Le store
    n'écrit qu'au plus une fois par intervalle de rafraîchissement.
    """

    account: Account
    expires_at: float
    refreshed: bool


class AuthService(Protocol):
    """Port d'authentification des surfaces publiques (spec #75).

    Les adaptateurs (Console, transport, Webhook) ne connaissent que cette
    interface, jamais le schéma SQL ni Argon2. L'implémentation vit dans
    ``server/adapters/auth``. Les Ticket Console (#76), les Jetons d'accès
    (#78) et le changement de mot de passe (#77) sont déclarés ici de façon
    additive ; les consommateurs de Jetons (#79 transport, #80 Webhook) n'ont
    que ``authenticate_token`` et ``mark_token_used`` à appeler.
    """

    @property
    def setup_required(self) -> bool:
        """Vrai tant qu'aucun Compte n'existe : l'instance n'est pas installée."""
        ...

    @property
    def ticket_ttl(self) -> int:
        """Durée de validité d'un Ticket Console, en secondes."""
        ...

    @property
    def password_min_length(self) -> int:
        """Longueur minimale du mot de passe imposée côté serveur."""
        ...

    def announce_installation_code(self) -> str | None:
        """Écrit le Code d'installation au démarrage, s'il en faut un.

        Sans Compte, génère (une fois par démarrage) le code à usage unique et
        l'émet dans le flux de traces — donc dans le journal. Avec un Compte,
        ne fait rien et renvoie ``None`` : le code ne doit jamais réapparaître.
        """
        ...

    async def install(
        self, code: str, username: str, password: str
    ) -> IssuedTicket:
        """Crée l'unique Compte et ouvre un Ticket Console.

        Lève ``AuthError`` : ``invalid_code`` (code erroné ou consommé),
        ``already_installed`` (un Compte existe), ``weak_password``,
        ``invalid_username`` ou ``too_many_attempts``.
        """
        ...

    async def login(self, username: str, password: str) -> IssuedTicket:
        """Vérifie les identifiants et ouvre un Ticket Console.

        Lève ``AuthError`` : ``invalid_credentials`` (même code pour un
        utilisateur inconnu et un mot de passe faux, pour ne rien révéler) ou
        ``too_many_attempts`` après le seuil d'échecs.
        """
        ...

    async def authenticate_ticket(self, secret: str) -> AuthenticatedTicket | None:
        """Résout un secret de cookie en Compte, ou ``None`` s'il est invalide.

        Un ticket révoqué ou expiré est refusé immédiatement ; un ticket actif
        voit son expiration repoussée au plus une fois par intervalle.
        """
        ...

    async def revoke_ticket(self, secret: str) -> None:
        """Révoque un Ticket Console (déconnexion du navigateur courant)."""
        ...

    async def list_tokens(self) -> list[AccessToken]:
        """Liste les Jetons d'accès, révoqués compris, sans secret ni empreinte."""
        ...

    async def create_token(
        self, *, name: str, kind: str, source: str | None = None
    ) -> IssuedAccessToken:
        """Crée un Jeton nommé et renvoie son secret en clair, une seule fois.

        ``kind`` vaut ``client`` (transport des Clients) ou ``source`` (Webhook) ;
        ``source`` est obligatoire pour ``source``, interdite pour ``client``.
        Lève ``AuthError`` : ``invalid_token_name``, ``invalid_kind``,
        ``invalid_source``.
        """
        ...

    async def revoke_token(self, token_id: str) -> AccessToken | None:
        """Révoque un Jeton ; ``None`` si l'identifiant est inconnu.

        La révocation prend effet à la validation suivante, sans redémarrage. Un
        Jeton déjà révoqué garde sa date de révocation d'origine.
        """
        ...

    async def authenticate_token(
        self, secret: str, kind: str
    ) -> AccessToken | None:
        """Résout un secret en Jeton de la nature demandée, ou ``None``.

        Refuse un secret inconnu, révoqué, ou d'une autre nature : un Jeton
        ``source`` ne vaut pas pour le transport, et réciproquement.
        """
        ...

    async def mark_token_used(self, token_id: str) -> None:
        """Enregistre la dernière utilisation, au plus une fois par minute.

        Le transport et le Webhook appellent ceci après chaque usage ; l'écriture
        est bornée par la résolution du store pour ne pas écrire par trame.
        """
        ...

    async def revoke_account_tickets(self, account_id: str) -> None:
        """Révoque tous les Tickets Console d'un Compte (« déconnecter partout »).

        L'effet est immédiat : chaque vérification relit la base, donc un Ticket
        révoqué est refusé dès la requête suivante, y compris pour l'upgrade du
        flux de Traces. Un Ticket déjà révoqué est ignoré.
        """
        ...

    async def change_password(
        self, secret: str, current_password: str, new_password: str
    ) -> None:
        """Change le mot de passe du Compte, preuve de l'actuel à l'appui.

        ``secret`` identifie le Ticket Console courant : il reste valide après
        le changement, tandis que **les autres** Tickets du Compte sont
        révoqués. Lève ``AuthError`` : ``invalid_password`` si le mot de passe
        actuel est faux, ``weak_password`` si le nouveau est trop court,
        ``unauthorized`` si le Ticket a disparu entre-temps.
        """
        ...
