"""Plugin ``notion`` : pages Notion par la REST (ADR local 0001).

Ce module ne connaît du serveur que le contrat public des plugins
(``plugin_api``) : il n'importe aucun interne. Il appelle la REST Notion avec
un jeton d'intégration statique et épingle ``Notion-Version``.

Au démarrage, ``create`` sonde ``GET /v1/users/me`` puis la page parente
configurée. Un jeton refusé ou un Notion injoignable lève
``PluginUnavailable`` : le chargeur trace ``failed``, démarre dégradé et
retente en tâche de fond (ADR-0014). Une page parente non partagée, elle, est
un défaut de configuration fatal. Un appel de Tool, lui, ne lève jamais : une
panne devient une phrase rendue au modèle.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import httpx2

from plugin_api import (
    PluginBinding,
    PluginContext,
    PluginUnavailable,
    ToolBinding,
    ToolSpec,
)

# Valeur sûre appliquée par le plugin quand ``base_url`` manque : le chargeur
# ne sait pas porter de défaut (ADR-0014).
DEFAULT_BASE_URL = "https://api.notion.com"

# Version d'API épinglée, jamais une clé de configuration (ADR local 0001).
NOTION_VERSION = "2026-03-11"
# Version Markdown demandée explicitement sur les endpoints Markdown.
MARKDOWN_VERSION = "v2"
# Budget HTTP fixe : un seul essai, aucun retry automatique.
REQUEST_TIMEOUT = 10.0

# Taille de page de ``POST /v1/search``.
SEARCH_PAGE_SIZE = 10

# Sentinelle rendue quand Notion ne donne aucun titre à une page.
UNTITLED_TITLE = "(sans titre)"

_SEARCH_DESCRIPTION = (
    "Cherche des pages Notion par titre et rend les candidats avec leur URL."
)

_READ_DESCRIPTION = (
    "Lit une page Notion en Markdown, par blocs, avec ses propriétés ; "
    "filtre éventuellement les paragraphes contenant un mot."
)

# Blocs de premier niveau rendus par appel : défaut 20, bornes 1..50.
DEFAULT_READ_LIMIT = 20
MAX_READ_LIMIT = 50

_TOOL_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Titre recherché, en mots.",
        },
        "cursor": {
            "type": "string",
            "description": "Curseur rendu par l'appel précédent.",
        },
    },
    "required": ["query"],
}

_READ_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "page": {
            "type": "string",
            "description": "Identifiant ou URL de la page.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Bloc (ou correspondance) de départ. Défaut 0.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Blocs (ou correspondances) par appel. Défaut 20.",
        },
        "query": {
            "type": "string",
            "description": "Mot à chercher dans les paragraphes de cette page.",
        },
    },
    "required": ["page"],
}

_UPDATE_DESCRIPTION = (
    "Ajoute du contenu à une page, remplace son corps sur confirmation, "
    "ou la renomme."
)

_UPDATE_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "page": {
            "type": "string",
            "description": "Identifiant ou URL de la page.",
        },
        "title": {
            "type": "string",
            "description": "Nouveau titre. Optionnel.",
        },
        "content": {
            "type": "string",
            "description": "Markdown à ajouter ou à substituer. Optionnel.",
        },
        "mode": {
            "type": "string",
            "enum": ["append", "replace"],
            "description": "Défaut : append.",
        },
        "confirm": {
            "type": "boolean",
            "description": "Obligatoire à true pour mode=replace.",
        },
    },
    "required": ["page"],
}

# Instruction d'usage posée dans le prompt de toute Session : quand appeler
# chaque Tool, ce qui exige confirmation, le sous-ensemble Markdown garanti et
# les limites assumées.
INSTRUCTION = (
    "Les Tools Notion ne voient que les pages partagées avec la connexion, "
    "et rien d'autre ; les bases de données et les data sources sont "
    "hors périmètre, et seul le titre s'écrit parmi les propriétés. "
    "N'invente jamais un identifiant ni une URL de page : n'utilise que ceux "
    "rendus par `search_pages` ou donnés par l'utilisateur.\n"
    "`search_pages` : quand l'utilisateur nomme une page sans identifiant ni "
    "URL. Si plusieurs titres se ressemblent, lis les titres candidats à voix "
    "haute et fais choisir ; suis `next_cursor` s'il reste des résultats.\n"
    "`read_page` : quand l'utilisateur veut le contenu, un détail, ou « la "
    "suite ». Reprends avec `offset` et `next_offset`. Utilise `query` pour "
    "retrouver un mot dans les paragraphes de cette page seulement, avec son "
    "titre de section ; ne cherche jamais dans tout le workspace.\n"
    "`create_page` : pour créer une page sous la page parente par défaut, ou "
    "sous un parent que l'utilisateur nomme ; confirme le parent quand il en "
    "nomme un.\n"
    "`update_page` : ajoute du contenu par défaut ; ne remplace le corps "
    "qu'après une confirmation explicite, avec `mode=replace` et "
    "`confirm=true`. Pour corriger un titre dicté, renomme.\n"
    "`trash_page` : seulement après confirmation explicite, après avoir lu le "
    "titre et annoncé les `child_pages` emportées.\n"
    "`restore_page` : pour annuler une mise à la corbeille.\n"
    "Markdown garanti en écriture : titres 1 à 4, paragraphes, listes à puces "
    "et numérotées, cases à cocher, citations, blocs de code avec un nom de "
    "langage court, gras, italique, barré et code en ligne, liens, tableaux "
    "GFM en pipe tables, et "
    '<columns><column ratio="50">…</column><column ratio="50">…</column>'
    "</columns> pour les colonnes. Limites : 2 000 caractères par texte "
    "enrichi, 100 blocs par ajout, 5 000 blocs à la création ; au-delà, "
    "Notion refuse plutôt que de couper en silence."
)

_UNREACHABLE_MESSAGE = (
    "Notion est injoignable. Réessaie dans un instant."
)
_TIMEOUT_MESSAGE = (
    "Notion n'a pas répondu à temps. Réessaie dans un instant."
)
_UNREADABLE_MESSAGE = (
    "Notion a renvoyé une réponse illisible. Réessaie dans un instant."
)
_TOKEN_MESSAGE = (
    "Notion a refusé le jeton d'intégration. "
    "Vérifie ATHENA_PLUGIN_NOTION_TOKEN, puis réessaie."
)
_CAPABILITY_MESSAGE = (
    "La connexion Notion n'a pas la capacité nécessaire pour cette action. "
    "Vérifie le partage de la page avec la connexion."
)
_NOT_FOUND_MESSAGE = (
    "Page introuvable ou non partagée avec la connexion Notion."
)
_RETRY_SIMPLER = "Réessaie avec un contenu plus simple."
_VALIDATION_MESSAGE = (
    "Notion a refusé la demande (contenu invalide ou page trop volumineuse). "
    + _RETRY_SIMPLER
)
_RATE_MESSAGE = (
    "Notion limite le débit des requêtes. Réessaie dans quelques instants."
)
_TEMPORARY_MESSAGE = (
    "Notion est temporairement indisponible. Réessaie dans un instant."
)
_WRITE_UNCERTAIN_MESSAGE = (
    "Le changement peut avoir été enregistré ; "
    "relis la page avant de réessayer."
)
_STATUS_MESSAGE = (
    "Notion a renvoyé une erreur (réponse {status}). "
    "Réessaie dans un instant."
)
_INVALID_PAGE_MESSAGE = (
    "Identifiant de page invalide : indique un identifiant Notion ou l'URL "
    "de la page."
)
_REPLACE_REFUSED_MESSAGE = (
    "Le remplacement du contenu exige une confirmation explicite. "
    "Relance l'appel avec confirm=true, ou utilise le mode append pour "
    "ajouter du contenu."
)
_REPLACE_EMPTY_MESSAGE = (
    "Le remplacement du contenu exige un contenu non vide. Indique le "
    "Markdown qui doit remplacer le corps de la page."
)
_REPLACE_REFUSED_BY_NOTION_MESSAGE = (
    "Notion a refusé le remplacement du contenu. Le plus souvent, ce refus "
    "protège une sous-page ou une base enfant que le remplacement aurait "
    "supprimée : elle n'a pas été touchée. Un Markdown invalide ou une page "
    "trop volumineuse provoquent le même refus. Rien n'a été perdu et le "
    "corps d'origine est intact."
)
_NO_TITLE_PROPERTY_MESSAGE = (
    "La page n'expose pas de propriété de titre : le renommage est impossible."
)
_TRUNCATED_AND_UNKNOWN_NOTE = (
    "Le contenu de la page est tronqué par Notion ; "
    "certains blocs n'ont pas pu être lus."
)
_TRUNCATED_NOTE = (
    "Le contenu de la page est tronqué par Notion ; la fin peut manquer."
)
_UNKNOWN_NOTE = "Certains blocs de la page n'ont pas pu être lus."

class Trace(Protocol):
    """Contrat de ``PluginContext.trace`` : genre, payload, niveau nommé."""

    def __call__(
        self,
        kind: str,
        payload: Mapping[str, object] | None = None,
        *,
        level: str = "info",
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _Settings:
    """Cible et réglages du plugin, secret compris (le jeton n'est jamais tracé)."""

    base_url: str
    token: str
    parent_page_id: str


async def create(context: PluginContext) -> PluginBinding:
    """Sonde Notion, puis rend les Tools du plugin.

    La sonde est la seule opération qui peut lever ``PluginUnavailable`` : un
    jeton refusé ou un Notion injoignable est une panne réparable, retentée par
    le chargeur. Une page parente non partagée est un défaut de configuration
    définitif, donc une erreur ordinaire.
    """
    settings = _settings(context)
    await _probe(settings, context.trace)
    return PluginBinding(
        name=context.name,
        tools=_tool_bindings(settings, context.trace),
        instruction=INSTRUCTION,
    )


def _settings(context: PluginContext) -> _Settings:
    """Applique les défauts sur la configuration déclarée par le manifeste."""
    config = context.config

    def text(key: str, default: str = "") -> str:
        return str(config.get(key, "")).strip() or default

    return _Settings(
        base_url=text("base_url", DEFAULT_BASE_URL).rstrip("/"),
        token=str(context.secrets.get("token", "")),
        parent_page_id=text("parent_page_id"),
    )


# Un identifiant Notion fait 32 caractères hexadécimaux, avec ou sans tirets.
_BARE_PAGE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_DASHED_PAGE_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_RUN = re.compile(r"[0-9a-fA-F]{32}")


def _page_id(raw: object) -> str | None:
    """Réduit un identifiant ou une URL Notion à son identifiant de page.

    Une URL est réduite à son dernier segment de 32 caractères hexadécimaux ;
    tout autre texte ne désigne aucune page. L'identifiant rendu est sans
    tirets, la forme que les chemins REST attendent.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if _BARE_PAGE_ID.fullmatch(text):
        return text.lower()
    if _DASHED_PAGE_ID.fullmatch(text):
        return text.replace("-", "").lower()
    candidate = text
    if candidate.lower().startswith("www."):
        candidate = "https://" + candidate
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    matches = _HEX_RUN.findall(text)
    return matches[-1].lower() if matches else None


async def _probe(settings: _Settings, trace: Trace) -> None:
    """``GET /v1/users/me`` puis ``GET /v1/pages/{parent}``.

    Seules l'authentification refusée et l'injoignabilité sont des pannes
    retentables. Un 5xx sur le compte est tracé puis ignoré : Notion répond,
    c'est sa sonde qui est en défaut, pas le plugin. La page parente, elle,
    doit exister et être partagée : son absence est un défaut de configuration,
    jamais une panne à retenter.
    """
    parent = _page_id(settings.parent_page_id)
    if parent is None:
        # Aucun appel HTTP : la configuration est refusée avant le réseau.
        raise RuntimeError(
            "configuration invalide : parent_page_id doit être un identifiant "
            "de page Notion (32 caractères hexadécimaux) ou une URL de page "
            "Notion"
        )

    async with _client(settings, REQUEST_TIMEOUT) as client:
        try:
            account = await client.get(settings.base_url + "/v1/users/me")
        except asyncio.CancelledError:
            raise
        except httpx2.TimeoutException as exc:
            _trace_probe_failed(trace, reason="timeout")
            raise PluginUnavailable(
                "notion probe failed: Notion did not answer in time"
            ) from exc
        except httpx2.RequestError as exc:
            _trace_probe_failed(trace, reason=type(exc).__name__)
            raise PluginUnavailable(
                f"notion probe failed: Notion is unreachable "
                f"({type(exc).__name__})"
            ) from exc

        if account.status_code in (401, 403):
            _trace_probe_failed(trace, status=account.status_code)
            raise PluginUnavailable(
                f"notion probe failed: Notion refused the token "
                f"(HTTP {account.status_code})"
            )
        if account.status_code >= 500:
            # Notion répond : sa sonde est en défaut, le plugin ne l'est pas.
            trace(
                "ProbeDegraded",
                {"status": account.status_code},
                level="warning",
            )
        elif account.status_code >= 400:
            _trace_probe_failed(trace, status=account.status_code)
            raise PluginUnavailable(
                f"notion probe failed: unexpected status "
                f"HTTP {account.status_code}"
            )

        try:
            page = await client.get(
                settings.base_url + f"/v1/pages/{parent}"
            )
        except asyncio.CancelledError:
            raise
        except httpx2.TimeoutException as exc:
            _trace_probe_failed(trace, reason="timeout")
            raise PluginUnavailable(
                "notion probe failed: the parent page did not answer in time"
            ) from exc
        except httpx2.RequestError as exc:
            _trace_probe_failed(trace, reason=type(exc).__name__)
            raise PluginUnavailable(
                f"notion probe failed: the parent page is unreachable "
                f"({type(exc).__name__})"
            ) from exc

        if page.status_code in (403, 404):
            trace(
                "ProbeFailed",
                {"status": page.status_code, "reason": "parent_page_id"},
                level="error",
            )
            raise RuntimeError(
                f"configuration invalide : parent_page_id {parent!r} est "
                "introuvable ou n'est pas partagé avec la connexion Notion ; "
                "partage la page avec la connexion, puis redémarre"
            )
        if page.status_code >= 500:
            # La page parente répond mal, mais le plugin reste utilisable :
            # chaque Tool dira la vérité au moment de l'appel.
            trace(
                "ProbeDegraded",
                {"status": page.status_code},
                level="warning",
            )
        elif page.status_code >= 400:
            _trace_probe_failed(trace, status=page.status_code)
            raise PluginUnavailable(
                f"notion probe failed: the parent page returned "
                f"HTTP {page.status_code}"
            )


def _trace_probe_failed(
    trace: Trace,
    *,
    reason: str | None = None,
    status: int | None = None,
) -> None:
    """Trace un échec de sonde sans jamais y placer le jeton."""
    payload: dict[str, object] = {}
    if reason is not None:
        payload["reason"] = reason
    if status is not None:
        payload["status"] = status
    trace("ProbeFailed", payload, level="warning")


def _client(settings: _Settings, timeout: float) -> httpx2.AsyncClient:
    """Client HTTP porteur du Bearer ; le jeton ne quitte jamais cette en-tête."""
    return httpx2.AsyncClient(
        headers={
            "Authorization": f"Bearer {settings.token}",
            "Notion-Version": NOTION_VERSION,
        },
        timeout=httpx2.Timeout(timeout),
    )


def _text(value: object) -> str:
    """Texte sûr d'un champ Notion absent ou mal typé."""
    return value if isinstance(value, str) else ""


def _page_title(page: Mapping[str, Any]) -> str:
    """Titre de la propriété ``type == "title"``, ou ``UNTITLED_TITLE``."""
    properties = page.get("properties")
    if isinstance(properties, dict):
        for prop in properties.values():
            if not isinstance(prop, dict) or prop.get("type") != "title":
                continue
            title = prop.get("title")
            if not isinstance(title, list):
                continue
            text = "".join(
                _text(part.get("plain_text"))
                for part in title
                if isinstance(part, dict)
            ).strip()
            if text:
                return text
    return UNTITLED_TITLE


def _page_summary(page: Mapping[str, Any]) -> dict[str, object]:
    """Projette un objet page sur la forme complète rendue au modèle."""
    return {
        "id": _text(page.get("id")),
        "title": _page_title(page),
        "url": _text(page.get("url")),
        "last_edited_time": _text(page.get("last_edited_time")),
    }


def _page_brief(
    page: Mapping[str, Any],
    *,
    fallback_id: str = "",
    fallback_title: str = "",
    fallback_url: str = "",
) -> dict[str, object]:
    """Forme ``page`` réduite à l'identité, avec replis explicites.

    Un corps partiel (création sans propriété, renommage sans URL) ne doit pas
    effacer ce que l'appelant savait déjà : chaque champ absent retombe sur son
    repli.
    """
    summary = _page_summary(page)
    title = str(summary["title"])
    if title == UNTITLED_TITLE and fallback_title:
        title = fallback_title
    return {
        "id": str(summary["id"]) or fallback_id,
        "title": title,
        "url": str(summary["url"]) or fallback_url,
    }


# -- Tool ``read_page`` --------------------------------------------------------


# Marqueur d'ouverture d'un bloc de code clôturé par le même caractère.
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
# Ligne de liste à puces ou numérotée, éventuellement indentée.
_LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
# Titre Markdown : un à six dièses en début de ligne.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
# Citation : jamais un paragraphe pour le filtre par mot.
_BLOCKQUOTE = re.compile(r"^\s*>")
# Séparateur horizontal (``---``, ``***``, ``___``), jamais un paragraphe.
_THEMATIC_BREAK = re.compile(
    r"^\s{0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$"
)
# Balise Notion ouvrant une région à garder entière (colonnes, tableau, etc.).
_CONTAINER_TAG = re.compile(r"^\s*<([a-z][a-z-]*)", re.IGNORECASE)
_CONTAINER_TAGS = frozenset(
    (
        "columns",
        "table",
        "callout",
        "toggle",
        "page",
        "database",
        "synced-block",
    )
)


def _starts_block(line: str) -> bool:
    """Vrai si la ligne ouvre un bloc de premier niveau."""
    if (
        _FENCE.match(line)
        or _HEADING.match(line)
        or _BLOCKQUOTE.match(line)
        or _THEMATIC_BREAK.match(line)
    ):
        return True
    if _LIST_LINE.match(line) or line.lstrip().startswith("|"):
        return True
    match = _CONTAINER_TAG.match(line)
    return bool(match and match.group(1).lower() in _CONTAINER_TAGS)


def _indent(line: str) -> int:
    """Largeur de l'indentation de tête, en caractères."""
    return len(line) - len(line.lstrip(" \t"))


def _list_run_end(lines: list[str], start: int) -> int:
    """Fin d'une suite de listes : une ligne non indentée la termine.

    Les continuations et les listes imbriquées, plus indentées que le premier
    item, restent dans la suite et seront rattachées à leur item.
    """
    base = _indent(lines[start])
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            break
        if _LIST_LINE.match(line) or _indent(line) > base:
            index += 1
            continue
        break
    return index


def _list_item_ranges(
    lines: list[str], start: int, end: int
) -> list[tuple[int, int]]:
    """Bornes d'une suite de listes, un bloc par item de premier niveau."""
    base = _indent(lines[start])
    bounds: list[tuple[int, int]] = []
    item_start = start
    for index in range(start + 1, end):
        if _LIST_LINE.match(lines[index]) and _indent(lines[index]) <= base:
            bounds.append((item_start, index))
            item_start = index
    bounds.append((item_start, end))
    return bounds


def _fence_block(lines: list[str], start: int) -> int:
    """Indice qui suit la clôture d'un bloc de code (ou la fin du texte)."""
    match = _FENCE.match(lines[start])
    if match is None:
        return start + 1
    marker = match.group(1)
    closing = re.compile(
        rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$"
    )
    index = start + 1
    while index < len(lines):
        if closing.match(lines[index]):
            return index + 1
        index += 1
    return len(lines)


def _container_block(lines: list[str], start: int, tag: str) -> int:
    """Indice qui suit la balise fermante de même nom, imbrication comprise.

    Seule la balise ouvrante compte : ``<column>`` imbriqué dans ``<columns>``
    ne clôt donc jamais le groupe trop tôt.
    """
    opening = re.compile(
        rf"<{re.escape(tag)}(?=[\s>/])", re.IGNORECASE
    )
    closing = re.compile(rf"</{re.escape(tag)}\s*>", re.IGNORECASE)
    depth = 0
    index = start
    while index < len(lines):
        line = lines[index]
        # Une balise auto-fermante ne change pas la profondeur.
        cleaned = re.sub(
            rf"<{re.escape(tag)}(?=[\s>/])[^>]*?/>",
            "",
            line,
            flags=re.IGNORECASE,
        )
        depth += len(opening.findall(cleaned)) - len(closing.findall(cleaned))
        index += 1
        if depth <= 0:
            break
    return index


def _markdown_ranges(markdown: str) -> list[tuple[int, int]]:
    """Bornes des blocs de premier niveau dans le texte Markdown d'origine.

    Un bloc de code, un tableau GFM et une région de balises Notion restent
    entiers ; chaque item de liste de premier niveau est un bloc, avec ses
    continuations indentées ; un titre ou un paragraphe est un bloc. Les bornes
    servent à rendre la tranche d'origine, donc les séparateurs de Notion (un
    seul saut de ligne entre deux blocs en ``v2``) sont préservés.
    """
    lines = markdown.splitlines()
    offsets: list[int] = []
    cursor = 0
    for raw in markdown.splitlines(keepends=True):
        offsets.append(cursor)
        cursor += len(raw)

    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        if _FENCE.match(line):
            end = _fence_block(lines, index)
        elif line.lstrip().startswith("|"):
            end = index + 1
            while end < len(lines) and lines[end].lstrip().startswith("|"):
                end += 1
        elif (
            (match := _CONTAINER_TAG.match(line))
            and match.group(1).lower() in _CONTAINER_TAGS
        ):
            end = _container_block(lines, index, match.group(1))
        elif _LIST_LINE.match(line):
            end = _list_run_end(lines, index)
            for item_start, item_end in _list_item_ranges(
                lines, index, end
            ):
                stop = offsets[item_end - 1] + len(lines[item_end - 1])
                ranges.append((offsets[item_start], stop))
            index = end
            continue
        elif _BLOCKQUOTE.match(line):
            end = index + 1
            while end < len(lines) and _BLOCKQUOTE.match(lines[end]):
                end += 1
        elif _THEMATIC_BREAK.match(line) or _HEADING.match(line):
            end = index + 1
        else:
            end = index + 1
            while (
                end < len(lines)
                and lines[end].strip()
                and not _starts_block(lines[end])
            ):
                end += 1

        stop = offsets[end - 1] + len(lines[end - 1])
        ranges.append((offsets[index], stop))
        index = end
    return ranges


def _markdown_blocks(markdown: str) -> list[str]:
    """Découpe un Markdown en blocs de premier niveau sans en couper un seul."""
    return [markdown[start:stop] for start, stop in _markdown_ranges(markdown)]


# Marques combinantes retirées pour comparer sans accents.
_COMBINING_MARKS = re.compile(r"[\u0300-\u036f]")


def _fold(text: str) -> str:
    """Forme comparée : minuscules, sans accents ; jamais la forme rendue."""
    decomposed = unicodedata.normalize("NFKD", text)
    return _COMBINING_MARKS.sub("", decomposed).casefold()


def _is_heading(block: str) -> bool:
    """Vrai si le bloc est un titre Markdown."""
    return bool(_HEADING.match(block))


def _is_paragraph(block: str) -> bool:
    """Vrai pour un paragraphe de premier niveau seulement.

    Un titre, une liste, une citation, un séparateur horizontal, un tableau, un
    bloc de code et une région de balises Notion ne sont jamais des
    paragraphes : le filtre par mot les ignore.
    """
    first = block.split("\n", 1)[0]
    if (
        _FENCE.match(first)
        or _HEADING.match(first)
        or _LIST_LINE.match(first)
        or _BLOCKQUOTE.match(first)
        or _THEMATIC_BREAK.match(first)
    ):
        return False
    if first.lstrip().startswith("|"):
        return False
    match = _CONTAINER_TAG.match(first)
    return not (match and match.group(1).lower() in _CONTAINER_TAGS)


def _query_matches(blocks: list[str], query: str) -> list[str]:
    """Paragraphes contenant le mot entier, chacun avec son titre de section.

    La comparaison ignore casse et accents, mais le texte rendu garde les
    siens. Le contexte est le dernier titre qui précède le paragraphe ; avant
    le premier titre, il n'y en a aucun.
    """
    folded = _fold(query)
    if not folded:
        return []
    pattern = re.compile(rf"(?<!\w){re.escape(folded)}(?!\w)")
    heading = ""
    matches: list[str] = []
    for block in blocks:
        if _is_heading(block):
            heading = block
            continue
        if not _is_paragraph(block) or not pattern.search(_fold(block)):
            continue
        matches.append(f"{heading}\n\n{block}" if heading else block)
    return matches


def _properties(page: Mapping[str, Any]) -> dict[str, str]:
    """Propriétés non vides rendues en texte, dans l'ordre de la page."""
    raw = page.get("properties")
    if not isinstance(raw, dict):
        return {}
    rendered: dict[str, str] = {}
    for name, prop in raw.items():
        if not isinstance(name, str) or not isinstance(prop, dict):
            continue
        text = _property_text(prop)
        if text:
            rendered[name] = text
    return rendered


def _property_text(prop: Mapping[str, Any]) -> str:
    """Rend une propriété Notion en texte ; un type inconnu est omis."""
    kind = prop.get("type")
    if kind == "title":
        return _rich_text(prop.get("title"))
    if kind == "rich_text":
        return _rich_text(prop.get("rich_text"))
    if kind in ("select", "status"):
        option = prop.get(kind)
        if not isinstance(option, dict):
            return ""
        return _text(option.get("name")).strip()
    if kind == "multi_select":
        return _multi_select_text(prop.get("multi_select"))
    if kind == "relation":
        return _relation_text(prop.get("relation"))
    if kind == "rollup":
        rollup = prop.get("rollup")
        return _rollup_text(rollup) if isinstance(rollup, dict) else ""
    if kind == "files":
        return _files_text(prop.get("files"))
    if kind == "unique_id":
        unique = prop.get("unique_id")
        return _unique_id_text(unique) if isinstance(unique, dict) else ""
    if kind == "date":
        date = prop.get("date")
        if not isinstance(date, dict):
            return ""
        return _text(date.get("start")).strip()
    if kind == "number":
        return _number_text(prop.get("number"))
    if kind == "checkbox":
        return "oui" if prop.get("checkbox") is True else "non"
    if kind in ("url", "email", "phone_number"):
        return _text(prop.get(kind)).strip()
    if kind == "formula":
        formula = prop.get("formula")
        return _formula_text(formula) if isinstance(formula, dict) else ""
    if kind == "people":
        return _people_text(prop.get("people"))
    return ""


def _number_text(value: object) -> str:
    """Nombre Notion en texte ; un booléen n'est jamais un nombre."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _formula_text(formula: Mapping[str, Any]) -> str:
    """Résultat d'une formule, quel que soit son type."""
    result = formula.get("result")
    if isinstance(result, bool):
        return "oui" if result else "non"
    if isinstance(result, dict):
        return _text(result.get("start")).strip()
    return _number_text(result)


def _people_text(people: object) -> str:
    """Noms des personnes, séparés par une virgule."""
    if not isinstance(people, list):
        return ""
    names = [
        _text(person.get("name")).strip()
        for person in people
        if isinstance(person, dict)
    ]
    return ", ".join(name for name in names if name)


def _multi_select_text(options: object) -> str:
    """Noms des options choisies, séparés par une virgule."""
    if not isinstance(options, list):
        return ""
    names = [
        _text(option.get("name")).strip()
        for option in options
        if isinstance(option, dict)
    ]
    return ", ".join(name for name in names if name)


def _relation_text(relations: object) -> str:
    """Nom de la page liée quand Notion le donne, sinon son identifiant."""
    if not isinstance(relations, list):
        return ""
    labels: list[str] = []
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        label = _text(relation.get("name")).strip() or _text(
            relation.get("id")
        ).strip()
        if label:
            labels.append(label)
    return ", ".join(labels)


def _rollup_text(rollup: Mapping[str, Any]) -> str:
    """Valeur calculée d'un rollup, selon le type que Notion rend."""
    kind = rollup.get("type")
    if kind == "number":
        return _number_text(rollup.get("number"))
    if kind == "date":
        date = rollup.get("date")
        return _text(date.get("start")).strip() if isinstance(date, dict) else ""
    if kind == "array":
        items = rollup.get("array")
        if not isinstance(items, list):
            return ""
        values = [
            _property_text(item) if isinstance(item, dict) else ""
            for item in items
        ]
        return ", ".join(value for value in values if value)
    return _number_text(rollup.get("number"))


def _files_text(files: object) -> str:
    """Noms des fichiers joints, séparés par une virgule."""
    if not isinstance(files, list):
        return ""
    names = [
        _text(file.get("name")).strip()
        for file in files
        if isinstance(file, dict)
    ]
    return ", ".join(name for name in names if name)


def _unique_id_text(unique: Mapping[str, Any]) -> str:
    """Identifiant unique Notion : préfixe et numéro, ou le numéro seul."""
    number = unique.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return ""
    prefix = _text(unique.get("prefix")).strip()
    return f"{prefix}-{number}" if prefix else str(number)


def _rich_text(items: object) -> str:
    """Concatène le ``plain_text`` d'un tableau de texte enrichi."""
    if not isinstance(items, list):
        return ""
    return "".join(
        _text(part.get("plain_text"))
        for part in items
        if isinstance(part, dict)
    ).strip()


def _reading_note(truncated: bool, unknown_ids: list[object]) -> str:
    """Phrase française posée quand Notion signale une lecture incomplète."""
    if truncated and unknown_ids:
        return _TRUNCATED_AND_UNKNOWN_NOTE
    if truncated:
        return _TRUNCATED_NOTE
    if unknown_ids:
        return _UNKNOWN_NOTE
    return ""


def _read_int(value: object, default: int) -> int:
    """Entier sûr d'un argument du modèle ; le défaut couvre le doute."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _read_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``read_page`` et son handler : un appel ne lève jamais."""

    async def handle(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        return await _read(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="read_page",
            description=_READ_DESCRIPTION,
            parameters=_READ_PARAMETERS,
        ),
        handler=handle,
    )


async def _read(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Lit une page en Markdown, paginée par blocs de premier niveau.

    Un appel ne lève jamais : une panne devient une phrase française. Un
    identifiant invalide est refusé avant le réseau. Aucun retry.
    """
    started = asyncio.get_running_loop().time()
    page = _page_id(arguments.get("page"))
    offset = max(0, _read_int(arguments.get("offset"), 0))
    limit = min(
        MAX_READ_LIMIT,
        max(1, _read_int(arguments.get("limit"), DEFAULT_READ_LIMIT)),
    )
    # Un filtre vide ou mal typé vaut absence de filtre : lecture paginée.
    raw_query = arguments.get("query")
    query = raw_query.strip() if isinstance(raw_query, str) else ""
    query = query or None

    if page is None:
        _trace_read(
            trace,
            None,
            offset,
            limit,
            started,
            query=query,
            reason="invalid page",
        )
        return _error(_INVALID_PAGE_MESSAGE)

    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            page_object = await _call(
                client, "GET", settings.base_url + f"/v1/pages/{page}"
            )
            markdown_object = await _call(
                client,
                "GET",
                settings.base_url + f"/v1/pages/{page}/markdown",
                params={"markdown_version": MARKDOWN_VERSION},
            )
    except _ActionFailed as failure:
        _trace_read(
            trace,
            page,
            offset,
            limit,
            started,
            query=query,
            reason=failure.reason,
        )
        return _error(failure.message)

    if not isinstance(markdown_object.get("markdown"), str):
        _trace_read(
            trace,
            page,
            offset,
            limit,
            started,
            query=query,
            reason="unreadable body",
        )
        return _error(_UNREADABLE_MESSAGE)

    markdown = markdown_object["markdown"]
    ranges = _markdown_ranges(markdown)
    if query is None:
        selected = ranges[offset : offset + limit]
        # La tranche d'origine garde les séparateurs de Notion.
        content = (
            markdown[selected[0][0] : selected[-1][1]] if selected else ""
        )
        remaining = len(ranges)
        match_count = 0
    else:
        candidates = _query_matches(_markdown_blocks(markdown), query)
        selected = candidates[offset : offset + limit]
        # Une correspondance est synthétisée : titre de section + paragraphe.
        content = "\n\n".join(selected)
        remaining = len(candidates)
        match_count = len(candidates)
    next_offset = offset + len(selected)
    has_more = next_offset < remaining
    truncated = markdown_object.get("truncated") is True
    unknown = markdown_object.get("unknown_block_ids")
    unknown_ids = list(unknown) if isinstance(unknown, list) else []

    payload: dict[str, object] = {
        "status": "ok",
        "page": {
            **_page_summary(page_object),
            "properties": _properties(page_object),
        },
        "content": content,
        "offset": offset,
        "next_offset": next_offset,
        "has_more": has_more,
        "total_blocks": len(ranges),
        "truncated": truncated,
        "unknown_block_ids": unknown_ids,
        "query": query,
        "match_count": match_count,
    }
    note = _reading_note(truncated, unknown_ids)
    if note:
        payload["note"] = note
    _trace_read(
        trace,
        page,
        offset,
        limit,
        started,
        query=query,
        match_count=match_count,
        blocks=len(selected),
        chars=len(content),
        total_blocks=len(ranges),
        truncated=truncated,
        unknown_ids=unknown_ids,
    )
    return payload


def _trace_read(
    trace: Trace,
    page: str | None,
    offset: int,
    limit: int,
    started: float,
    *,
    query: str | None = None,
    match_count: int = 0,
    blocks: int = 0,
    chars: int = 0,
    total_blocks: int = 0,
    truncated: bool = False,
    unknown_ids: list[object] | None = None,
    reason: str | None = None,
) -> None:
    """Trace une lecture : comptes, durée, statut ; jamais le contenu.

    Avec un filtre, la Trace porte aussi le mot cherché et le nombre de
    correspondances ; le contenu, lui, n'y entre jamais.
    """
    entry: dict[str, object] = {
        "page": page,
        "offset": offset,
        "limit": limit,
        "blocks": blocks,
        "chars": chars,
        "total_blocks": total_blocks,
        "truncated": truncated,
        "unknown_block_ids": list(unknown_ids or ()),
        "duration_ms": round(
            (asyncio.get_running_loop().time() - started) * 1000, 1
        ),
        "status": "error" if reason is not None else "ok",
    }
    if query is not None:
        entry["query"] = query
        entry["match_count"] = match_count
    if reason is not None:
        entry["reason"] = reason
    trace("ReadPage", entry)


# -- Tool ``search_pages`` -----------------------------------------------------


def _tool_bindings(
    settings: _Settings, trace: Trace
) -> tuple[ToolBinding, ...]:
    """Les six Tools de page du plugin, déclarés à chaque Connexion Live."""
    return (
        _search_tool(settings, trace),
        _create_tool(settings, trace),
        _read_tool(settings, trace),
        _update_tool(settings, trace),
        _trash_tool(settings, trace),
        _restore_tool(settings, trace),
    )


def _search_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``search_pages`` et son handler : un appel ne lève jamais."""

    async def handle(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        return await _search(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="search_pages",
            description=_SEARCH_DESCRIPTION,
            parameters=_TOOL_PARAMETERS,
        ),
        handler=handle,
    )


def _search_body(query: str, cursor: object) -> dict[str, object]:
    """Corps exact de ``POST /v1/search`` : filtre page, tri, curseur."""
    body: dict[str, object] = {
        "query": query,
        "filter": {"property": "object", "value": "page"},
        "sort": {
            "direction": "descending",
            "timestamp": "last_edited_time",
        },
        "page_size": SEARCH_PAGE_SIZE,
    }
    if isinstance(cursor, str) and cursor:
        body["start_cursor"] = cursor
    return body


def _search_page(
    body: object,
) -> tuple[list[dict[str, object]], bool, str | None]:
    """Projette la réponse de recherche : dix pages au plus, ordre conservé."""
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ValueError("unexpected search body")
    results: list[dict[str, object]] = []
    for item in body["results"]:
        if len(results) >= SEARCH_PAGE_SIZE:
            break
        if isinstance(item, dict):
            results.append(_page_summary(item))
    next_cursor = body.get("next_cursor")
    if not isinstance(next_cursor, str) or not next_cursor:
        next_cursor = None
    return results, bool(body.get("has_more")), next_cursor


async def _search(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Cherche des pages par titre, ou rend une phrase française.

    Un appel ne lève jamais : une panne devient une phrase que le modèle peut
    dire à haute voix. Aucun retry : 429 et 529 rendent une phrase de débit.
    """
    query = str(arguments.get("query", "")).strip()
    body = _search_body(query, arguments.get("cursor"))
    started = asyncio.get_running_loop().time()
    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            response = await _call(
                client, "POST", settings.base_url + "/v1/search", body=body
            )
        results, has_more, next_cursor = _search_page(response)
    except _ActionFailed as failure:
        _trace_search(trace, query, started, reason=failure.reason)
        return _error(failure.message)
    except (TypeError, ValueError):
        _trace_search(trace, query, started, reason="unreadable body")
        return _error(_UNREADABLE_MESSAGE)

    payload = {
        "status": "ok",
        "count": len(results),
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }
    _trace_search(
        trace,
        query,
        started,
        results=len(results),
        has_more=has_more,
    )
    return payload


def _trace_search(
    trace: Trace,
    query: str,
    started: float,
    *,
    results: int = 0,
    has_more: bool = False,
    reason: str | None = None,
) -> None:
    """Trace une recherche : requête, résultats, pagination, durée, statut."""
    entry: dict[str, object] = {
        "query": query,
        "results": results,
        "has_more": has_more,
        "duration_ms": round(
            (asyncio.get_running_loop().time() - started) * 1000, 1
        ),
        "status": "error" if reason is not None else "ok",
    }
    if reason is not None:
        entry["reason"] = reason
    trace("Search", entry)


# -- Tool ``update_page`` ------------------------------------------------------


def _update_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``update_page`` et son handler : un appel ne lève jamais."""

    async def handle(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        return await _update(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="update_page",
            description=_UPDATE_DESCRIPTION,
            parameters=_UPDATE_PARAMETERS,
        ),
        handler=handle,
    )


def _update_mode(raw: object) -> str:
    """Mode effectif : ``replace`` seulement sur demande explicite."""
    if isinstance(raw, str) and raw.strip().lower() == "replace":
        return "replace"
    return "append"


def _title_key(page: Mapping[str, Any]) -> str | None:
    """Clé de la propriété dont ``type == "title"``, ou ``None``."""
    properties = page.get("properties")
    if isinstance(properties, dict):
        for key, prop in properties.items():
            if isinstance(prop, dict) and prop.get("type") == "title":
                return key if isinstance(key, str) else None
    return None


def _merge_page(
    base: dict[str, object], page: object
) -> dict[str, object]:
    """Titre et URL rendus par un renommage, sans écraser par du vide."""
    if not isinstance(page, dict):
        return base
    return _page_brief(
        page,
        fallback_id=str(base.get("id", "")),
        fallback_title=str(base.get("title", "")),
        fallback_url=str(base.get("url", "")),
    )


def _append_body(content: str) -> dict[str, object]:
    """Corps exact de l'ajout : ``insert_content`` en fin de page."""
    return {
        "type": "insert_content",
        "insert_content": {
            "content": content,
            "position": {"type": "end"},
        },
        "markdown_version": MARKDOWN_VERSION,
    }


def _replace_body(content: str) -> dict[str, object]:
    """Corps exact du remplacement : suppression de contenu toujours refusée."""
    return {
        "type": "replace_content",
        "replace_content": {
            "new_str": content,
            "allow_deleting_content": False,
        },
        "markdown_version": MARKDOWN_VERSION,
    }


def _rename_body(key: str, title: str) -> dict[str, object]:
    """Corps exact du renommage, sur la propriété de titre découverte."""
    return {
        "properties": {
            key: {
                "title": [
                    {"type": "text", "text": {"content": title}}
                ]
            }
        }
    }


def _partial_message(
    title_failure: str | None,
    content_failure: str | None,
    *,
    mode: str = "append",
    uncertain: bool = False,
) -> str:
    """Phrase d'un succès partiel : ce qui a réussi, ce qui a échoué.

    ``uncertain`` vient de l'appelant, jamais d'une inspection de la phrase :
    une écriture 503/504 porte déjà l'invitation à relire.
    """
    verb = "remplacé" if mode == "replace" else "ajouté"
    if title_failure and content_failure:
        message = (
            "Ni le titre ni le contenu n'ont pu être mis à jour. "
            f"{title_failure} {content_failure}"
        )
    elif title_failure:
        message = (
            f"Le contenu a été {verb}, mais le titre n'a pas pu être "
            f"modifié. {title_failure}"
        )
    else:
        message = (
            "Le titre a été mis à jour, mais le contenu n'a pas pu être "
            f"{verb}. {content_failure}"
        )
    if not uncertain:
        message += " Relis la page avant de réessayer."
    return message


async def _update(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Ajoute du contenu, renomme, et dit clairement un succès partiel.

    La page est lue d'abord pour découvrir la clé de sa propriété de titre et
    rendre son identité. Le renommage et l'écriture du corps sont deux
    opérations séparées, tentées indépendamment ; aucune n'est rejouée.

    Le remplacement est refusé sans ``confirm: true`` et sans contenu, avant
    tout appel HTTP. Avec confirmation, il envoie ``replace_content`` avec
    ``allow_deleting_content: false`` : une sous-page n'est jamais détachée.
    """
    started = asyncio.get_running_loop().time()
    page_ref = _page_id(arguments.get("page"))
    if page_ref is None:
        _trace_update(trace, None, "append", started, reason="invalid page")
        return _error(_INVALID_PAGE_MESSAGE)

    mode = _update_mode(arguments.get("mode"))
    if mode == "replace" and arguments.get("confirm") is not True:
        _trace_update(
            trace, page_ref, mode, started, reason="replace refused"
        )
        return _error(_REPLACE_REFUSED_MESSAGE)

    raw_title = arguments.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    raw_content = arguments.get("content")
    content = (
        raw_content
        if isinstance(raw_content, str) and raw_content.strip()
        else ""
    )

    if mode == "replace" and not content:
        _trace_update(
            trace, page_ref, mode, started, reason="empty content"
        )
        return _error(_REPLACE_EMPTY_MESSAGE)

    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            page = await _call(
                client, "GET", settings.base_url + f"/v1/pages/{page_ref}"
            )
    except _ActionFailed as failure:
        _trace_update(
            trace, page_ref, mode, started, reason=failure.reason
        )
        return _error(failure.message)

    summary = _page_brief(page)
    key = _title_key(page)
    title_updated = False
    content_updated = False
    title_failure: str | None = None
    content_failure: str | None = None
    title_reason: str | None = None
    content_reason: str | None = None
    title_uncertain = False
    content_uncertain = False
    warnings: list[object] = []

    if title:
        if key is None:
            title_failure = _NO_TITLE_PROPERTY_MESSAGE
            title_reason = "no title property"
        else:
            try:
                async with _client(settings, REQUEST_TIMEOUT) as client:
                    result = await _call(
                        client,
                        "PATCH",
                        settings.base_url + f"/v1/pages/{page_ref}",
                        body=_rename_body(key, title),
                        write=True,
                    )
                title_updated = True
                summary = _merge_page(summary, result)
            except _ActionFailed as failure:
                title_failure = failure.message
                title_reason = failure.reason
                title_uncertain = failure.status in (503, 504)

    if content:
        body = (
            _replace_body(content)
            if mode == "replace"
            else _append_body(content)
        )
        try:
            async with _client(settings, REQUEST_TIMEOUT) as client:
                result = await _call(
                    client,
                    "PATCH",
                    settings.base_url + f"/v1/pages/{page_ref}/markdown",
                    body=body,
                    write=True,
                )
            content_updated = True
            if isinstance(result.get("warnings"), list):
                warnings = result["warnings"]
        except _ActionFailed as failure:
            if mode == "replace" and failure.status == 400:
                content_failure = _REPLACE_REFUSED_BY_NOTION_MESSAGE
            else:
                content_failure = failure.message
            content_reason = failure.reason
            content_uncertain = failure.status in (503, 504)

    chars = (len(title) if title_updated else 0) + (
        len(content) if content_updated else 0
    )
    flags: dict[str, object] = {
        "page": summary,
        "mode": mode,
        "title_updated": title_updated,
        "content_updated": content_updated,
        "warnings": warnings,
    }

    if title and content and (title_failure or content_failure):
        reason = ", ".join(
            entry
            for entry in (title_reason, content_reason)
            if entry is not None
        )
        _trace_update(
            trace,
            page_ref,
            mode,
            started,
            title_updated=title_updated,
            content_updated=content_updated,
            chars=chars,
            warnings=_warning_codes(warnings),
            warnings_count=len(warnings),
            reason=reason or "partial",
        )
        return {
            "status": "error",
            "message": _partial_message(
                title_failure,
                content_failure,
                mode=mode,
                uncertain=title_uncertain or content_uncertain,
            ),
            **flags,
        }

    failure = title_failure or content_failure
    if failure is not None:
        _trace_update(
            trace,
            page_ref,
            mode,
            started,
            title_updated=title_updated,
            content_updated=content_updated,
            chars=chars,
            warnings=_warning_codes(warnings),
            warnings_count=len(warnings),
            reason=title_reason or content_reason,
        )
        return _error(failure)

    _trace_update(
        trace,
        page_ref,
        mode,
        started,
        title_updated=title_updated,
        content_updated=content_updated,
        chars=chars,
        warnings=_warning_codes(warnings),
        warnings_count=len(warnings),
    )
    return {"status": "ok", **flags}


def _warning_codes(warnings: list[object]) -> list[str]:
    """Codes Notion seuls : un ``message`` peut citer le contenu de la page."""
    codes: list[str] = []
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        code = _text(warning.get("code")).strip()
        if code:
            codes.append(code)
    return codes


def _trace_update(
    trace: Trace,
    page: str | None,
    mode: str,
    started: float,
    *,
    title_updated: bool = False,
    content_updated: bool = False,
    chars: int = 0,
    warnings: list[str] | None = None,
    warnings_count: int | None = None,
    reason: str | None = None,
) -> None:
    """Trace un ``update_page`` : jamais le contenu, jamais le jeton."""
    codes = list(warnings or ())
    count = len(codes) if warnings_count is None else warnings_count
    entry: dict[str, object] = {
        "page": page,
        "mode": mode,
        "title_updated": title_updated,
        "content_updated": content_updated,
        "chars": chars,
        "warnings": codes,
        "warnings_count": count,
        "duration_ms": round(
            (asyncio.get_running_loop().time() - started) * 1000, 1
        ),
        "status": "error" if reason is not None else "ok",
    }
    if reason is not None:
        entry["reason"] = reason
    trace("UpdatePage", entry)


# -- Tools ``trash_page`` et ``restore_page`` ---------------------------------


_TRASH_DESCRIPTION = "Met une page Notion à la corbeille, de façon réversible."
_RESTORE_DESCRIPTION = "Sort une page Notion de la corbeille."

_TRASH_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "page": {
            "type": "string",
            "description": "Identifiant ou URL de la page.",
        },
        "confirm": {
            "type": "boolean",
            "description": "Doit valoir true ; sinon le Tool refuse.",
        },
    },
    "required": ["page"],
}

_RESTORE_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "page": {
            "type": "string",
            "description": "Identifiant ou URL de la page en corbeille.",
        },
    },
    "required": ["page"],
}

# Nombre de blocs enfants lus par page (maximum accepté par Notion).
BLOCKS_PAGE_SIZE = 100


class _ActionFailed(Exception):
    """Échec d'un appel : phrase française, motif de Trace et statut éventuel."""

    def __init__(
        self, message: str, reason: str, status: int | None = None
    ) -> None:
        super().__init__(reason)
        self.message = message
        self.reason = reason
        self.status = status


def _trash_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``trash_page`` et son handler : un appel ne lève jamais."""

    async def handle(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        return await _trash(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="trash_page",
            description=_TRASH_DESCRIPTION,
            parameters=_TRASH_PARAMETERS,
        ),
        handler=handle,
    )


def _restore_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``restore_page`` et son handler : un appel ne lève jamais."""

    async def handle(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        return await _restore(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="restore_page",
            description=_RESTORE_DESCRIPTION,
            parameters=_RESTORE_PARAMETERS,
        ),
        handler=handle,
    )


def _trash_confirmation_message(title: str, child_pages: int) -> str:
    """Phrase qui annonce la page et ses sous-pages avant toute écriture."""
    if child_pages:
        return (
            f"La page « {title} » et ses {child_pages} sous-pages seront "
            "mises à la corbeille. Redemande-moi la suppression avec "
            "confirm=true pour confirmer."
        )
    return (
        f"La page « {title} » sera mise à la corbeille. "
        "Redemande-moi la suppression avec confirm=true pour confirmer."
    )


async def _call(
    client: httpx2.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    body: Mapping[str, object] | None = None,
    write: bool = False,
) -> dict[str, object]:
    """Un appel REST Notion, ou une ``_ActionFailed`` portant la phrase.

    Un délai, un transport muet, un corps illisible et un statut d'erreur
    deviennent tous une phrase française ; aucun essai n'est rejoué. ``write``
    ne change qu'une chose : un 503/504 y est une écriture incertaine.
    """
    try:
        response = await client.request(method, url, params=params, json=body)
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException as exc:
        raise _ActionFailed(_TIMEOUT_MESSAGE, "timeout") from exc
    except httpx2.RequestError as exc:
        raise _ActionFailed(_UNREACHABLE_MESSAGE, type(exc).__name__) from exc
    except Exception as exc:  # défense : le handler ne lève jamais
        raise _ActionFailed(_UNREACHABLE_MESSAGE, type(exc).__name__) from exc

    if response.status_code >= 400:
        raise _ActionFailed(
            _status_error(response.status_code, write=write),
            f"HTTP {response.status_code}",
            response.status_code,
        )
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise _ActionFailed(_UNREADABLE_MESSAGE, "unreadable body") from exc
    if not isinstance(payload, dict):
        raise _ActionFailed(_UNREADABLE_MESSAGE, "unreadable body")
    return payload


async def _page_object(
    client: httpx2.AsyncClient, settings: _Settings, page: str
) -> dict[str, object]:
    """``GET /v1/pages/{id}`` : titre, URL et propriétés de la page."""
    return await _call(client, "GET", settings.base_url + f"/v1/pages/{page}")


async def _child_page_count(
    client: httpx2.AsyncClient, settings: _Settings, page: str
) -> int:
    """Compte les blocs ``child_page`` en suivant le curseur Notion."""
    url = settings.base_url + f"/v1/blocks/{page}/children"
    count = 0
    cursor: str | None = None
    while True:
        params = {"page_size": str(BLOCKS_PAGE_SIZE)}
        if cursor is not None:
            params["start_cursor"] = cursor
        body = await _call(client, "GET", url, params=params)
        results = body.get("results")
        if not isinstance(results, list):
            raise _ActionFailed(_UNREADABLE_MESSAGE, "unreadable body")
        count += sum(
            1
            for block in results
            if isinstance(block, dict) and block.get("type") == "child_page"
        )
        next_cursor = body.get("next_cursor")
        if (
            not body.get("has_more")
            or not isinstance(next_cursor, str)
            or not next_cursor
        ):
            return count
        cursor = next_cursor


async def _trash(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Met une page à la corbeille, après lecture et confirmation explicite.

    Sans ``confirm: true``, le Tool ne fait aucune écriture : il lit la page et
    ses sous-pages, puis rend une phrase qui les annonce au modèle.
    """
    started = asyncio.get_running_loop().time()
    page = _page_id(arguments.get("page"))
    if page is None:
        _trace_trash(trace, page=None, started=started, reason="invalid page")
        return _error(_INVALID_PAGE_MESSAGE)
    confirmed = arguments.get("confirm") is True
    child_pages: int | None = None

    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            page_body = await _page_object(client, settings, page)
            reference = _page_brief(page_body, fallback_id=page)
            child_pages = await _child_page_count(client, settings, page)
            if not confirmed:
                _trace_trash(
                    trace,
                    page=page,
                    started=started,
                    child_pages=child_pages,
                    reason="confirmation",
                )
                return {
                    "status": "error",
                    "message": _trash_confirmation_message(
                        str(reference["title"]), child_pages
                    ),
                    "page": reference,
                    "child_pages": child_pages,
                }
            await _call(
                client,
                "PATCH",
                settings.base_url + f"/v1/pages/{page}",
                body={"in_trash": True},
                write=True,
            )
    except _ActionFailed as failure:
        _trace_trash(
            trace,
            page=page,
            started=started,
            child_pages=child_pages,
            reason=failure.reason,
        )
        return _error(failure.message)

    _trace_trash(trace, page=page, started=started, child_pages=child_pages)
    return {
        "status": "ok",
        "page": reference,
        "child_pages": child_pages,
        "restorable": True,
    }


async def _restore(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Sort une page de la corbeille et rend son identifiant, titre et URL."""
    started = asyncio.get_running_loop().time()
    page = _page_id(arguments.get("page"))
    if page is None:
        _trace_restore(
            trace, page=None, started=started, reason="invalid page"
        )
        return _error(_INVALID_PAGE_MESSAGE)

    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            body = await _call(
                client,
                "PATCH",
                settings.base_url + f"/v1/pages/{page}",
                body={"in_trash": False},
                write=True,
            )
            if (
                _text(body.get("url"))
                and isinstance(body.get("properties"), dict)
            ):
                reference = _page_brief(body, fallback_id=page)
            else:
                # Le corps de l'écriture ne portait pas la page : on la relit.
                page_body = await _page_object(client, settings, page)
                reference = _page_brief(page_body, fallback_id=page)
    except _ActionFailed as failure:
        _trace_restore(
            trace, page=page, started=started, reason=failure.reason
        )
        return _error(failure.message)

    _trace_restore(trace, page=page, started=started)
    return {"status": "ok", "page": reference}


def _trace_trash(
    trace: Trace,
    *,
    page: str | None,
    started: float,
    child_pages: int | None = None,
    reason: str | None = None,
) -> None:
    """Trace une mise à la corbeille : page, sous-pages, durée, statut."""
    extra = {} if child_pages is None else {"child_pages": child_pages}
    _trace_page_action(
        trace,
        "TrashPage",
        page=page,
        started=started,
        extra=extra,
        reason=reason,
    )


def _trace_restore(
    trace: Trace,
    *,
    page: str | None,
    started: float,
    reason: str | None = None,
) -> None:
    """Trace une restauration : page, durée, statut."""
    _trace_page_action(
        trace,
        "RestorePage",
        page=page,
        started=started,
        extra={},
        reason=reason,
    )


def _trace_page_action(
    trace: Trace,
    kind: str,
    *,
    page: str | None,
    started: float,
    extra: Mapping[str, object],
    reason: str | None,
) -> None:
    """Trace commune aux actions de corbeille : ni contenu, ni jeton."""
    entry: dict[str, object] = dict(extra)
    if page is not None:
        entry["page"] = page
    entry["duration_ms"] = round(
        (asyncio.get_running_loop().time() - started) * 1000, 1
    )
    entry["status"] = "error" if reason is not None else "ok"
    if reason is not None:
        entry["reason"] = reason
    trace(kind, entry)


# -- Tool ``create_page`` ------------------------------------------------------


_CREATE_DESCRIPTION = (
    "Crée une page Notion sous une page parente, avec un titre et un corps "
    "Markdown."
)

_CREATE_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Titre de la page.",
        },
        "content": {
            "type": "string",
            "description": "Corps en Markdown Notion (v2). Optionnel.",
        },
        "parent": {
            "type": "string",
            "description": (
                "Identifiant ou URL d'une page parente. "
                "Défaut : la page configurée."
            ),
        },
    },
    "required": ["title"],
}

_CREATE_VALIDATION_MESSAGE = (
    "Notion a refusé le Markdown de la page (Markdown invalide, plus de "
    "5 000 blocs ou limite de taille atteinte). " + _RETRY_SIMPLER
)


def _create_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``create_page`` et son handler : un appel ne lève jamais."""

    async def handle(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        return await _create(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="create_page",
            description=_CREATE_DESCRIPTION,
            parameters=_CREATE_PARAMETERS,
        ),
        handler=handle,
    )


def _create_parent(settings: _Settings, raw: object) -> str | None:
    """Parent demandé, ou page configurée quand ``parent`` est vide."""
    if raw is None:
        return _page_id(settings.parent_page_id)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return _page_id(text or settings.parent_page_id)


def _create_body(parent: str, title: str, content: str) -> dict[str, object]:
    """Corps exact de ``POST /v1/pages``.

    La propriété de titre est envoyée explicitement. Le Markdown et sa version
    n'apparaissent que s'il y a un corps : la version seule est un 400.
    """
    body: dict[str, object] = {
        "parent": {"type": "page_id", "page_id": parent},
        "properties": {
            "title": {
                "type": "title",
                "title": [
                    {
                        "type": "text",
                        "text": {"content": title},
                    }
                ],
            }
        },
    }
    if content:
        body["markdown"] = content
        body["markdown_version"] = MARKDOWN_VERSION
    return body


async def _create(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Crée une page, ou rend une phrase française ; ne lève jamais."""
    title_value = arguments.get("title")
    title = title_value.strip() if isinstance(title_value, str) else ""
    content_value = arguments.get("content")
    content = content_value if isinstance(content_value, str) else ""
    parent = _create_parent(settings, arguments.get("parent"))
    started = asyncio.get_running_loop().time()

    if parent is None:
        # Aucun appel HTTP : l'identifiant est refusé avant le réseau.
        _trace_create(
            trace,
            parent=None,
            title=title,
            content=content,
            started=started,
            reason="invalid parent",
        )
        return _error(_INVALID_PAGE_MESSAGE)

    body = _create_body(parent, title, content)
    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            page = await _call(
                client,
                "POST",
                settings.base_url + "/v1/pages",
                body=body,
                write=True,
            )
    except _ActionFailed as failure:
        _trace_create(
            trace,
            parent=parent,
            title=title,
            content=content,
            started=started,
            reason=failure.reason,
        )
        if failure.status == 400:
            return _error(_CREATE_VALIDATION_MESSAGE)
        return _error(failure.message)

    summary = _page_brief(page, fallback_title=title)
    _trace_create(
        trace,
        parent=parent,
        title=title,
        content=content,
        started=started,
        page_id=str(summary["id"]) or None,
    )
    return {"status": "ok", "page": summary}


def _trace_create(
    trace: Trace,
    *,
    parent: str | None,
    title: str,
    content: str,
    started: float,
    page_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Trace une création sans jamais y placer le texte de la page."""
    entry: dict[str, object] = {
        "parent": parent,
        "title_chars": len(title),
        "content_chars": len(content),
        "duration_ms": round(
            (asyncio.get_running_loop().time() - started) * 1000, 1
        ),
        "status": "error" if reason is not None else "ok",
    }
    if page_id is not None:
        entry["page"] = page_id
    if reason is not None:
        entry["reason"] = reason
    trace("CreatePage", entry)


def _status_error(status: int, *, write: bool = False) -> str:
    """Phrase française d'un statut HTTP Notion, jamais une exception.

    ``write`` distingue une écriture incertaine : sur 503/504, elle a pu être
    enregistrée, donc la phrase invite à relire avant de réessayer.
    """
    if status == 401:
        return _TOKEN_MESSAGE
    if status == 403:
        return _CAPABILITY_MESSAGE
    if status == 404:
        return _NOT_FOUND_MESSAGE
    if status == 400:
        return _VALIDATION_MESSAGE
    if status in (429, 529):
        return _RATE_MESSAGE
    if status in (503, 504):
        return _WRITE_UNCERTAIN_MESSAGE if write else _TEMPORARY_MESSAGE
    return _STATUS_MESSAGE.format(status=status)


def _error(message: str) -> Mapping[str, object]:
    return {"status": "error", "message": message}
