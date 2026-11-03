"""Plugin ``notion`` : pages Notion par la REST (ADR-0019).

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
from dataclasses import dataclass
from typing import Any, Callable, Mapping
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

# Version d'API épinglée, jamais une clé de configuration (ADR-0019).
NOTION_VERSION = "2026-03-11"
# Version Markdown demandée explicitement sur les endpoints Markdown.
MARKDOWN_VERSION = "v2"
# Budget HTTP fixe : un seul essai, aucun retry automatique (spec #111).
REQUEST_TIMEOUT = 10.0

# Taille de page de ``POST /v1/search`` (spec #111).
SEARCH_PAGE_SIZE = 10

_SEARCH_DESCRIPTION = (
    "Cherche des pages Notion par titre et rend les candidats avec leur URL."
)

_READ_DESCRIPTION = (
    "Lit une page Notion en Markdown, par blocs, avec ses propriétés."
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
    },
    "required": ["page"],
}

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
_VALIDATION_MESSAGE = (
    "Notion a refusé la demande (contenu invalide ou page trop volumineuse). "
    "Réessaie avec un contenu plus simple."
)
_RATE_MESSAGE = (
    "Notion limite le débit des requêtes. Réessaie dans quelques instants."
)
_TEMPORARY_MESSAGE = (
    "Notion est temporairement indisponible. Réessaie dans un instant."
)
_WRITE_UNCERTAIN_MESSAGE = (
    "Notion a peut-être enregistré le changement ; "
    "relis la page avant de réessayer."
)
_STATUS_MESSAGE = (
    "Notion a renvoyé une erreur (réponse {status}). "
    "Réessaie dans un instant."
)
_INVALID_PAGE_MESSAGE = (
    "L'identifiant ou l'URL de la page est invalide. "
    "Donne un identifiant Notion ou une URL de page."
)
_TRUNCATED_AND_UNKNOWN_NOTE = (
    "Le contenu de la page est tronqué par Notion ; "
    "certains blocs n'ont pas pu être lus."
)
_TRUNCATED_NOTE = (
    "Le contenu de la page est tronqué par Notion ; la fin peut manquer."
)
_UNKNOWN_NOTE = "Certains blocs de la page n'ont pas pu être lus."

Trace = Callable[..., None]


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
        if page.status_code >= 400:
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
    """Titre de la propriété ``type == "title"``, ou ``(sans titre)``."""
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
    return "(sans titre)"


def _page_summary(page: Mapping[str, Any]) -> dict[str, object]:
    """Projette un objet page sur la forme rendue au modèle."""
    return {
        "id": _text(page.get("id")),
        "title": _page_title(page),
        "url": _text(page.get("url")),
        "last_edited_time": _text(page.get("last_edited_time")),
    }


# -- Tool ``read_page`` --------------------------------------------------------


# Marqueur d'ouverture d'un bloc de code clôturé par le même caractère.
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
# Ligne de liste à puces ou numérotée, éventuellement indentée.
_LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
# Titre Markdown : un à six dièses en début de ligne.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
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
    if _FENCE.match(line) or _HEADING.match(line):
        return True
    if _LIST_LINE.match(line) or line.lstrip().startswith("|"):
        return True
    match = _CONTAINER_TAG.match(line)
    return bool(match and match.group(1).lower() in _CONTAINER_TAGS)


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


def _markdown_blocks(markdown: str) -> list[str]:
    """Découpe un Markdown en blocs de premier niveau sans en couper un seul.

    Un bloc de code, un tableau GFM, une région de balises Notion et une suite
    de lignes de liste restent entiers ; un titre ou un paragraphe est un bloc.
    """
    lines = markdown.splitlines()
    blocks: list[str] = []
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
            end = index + 1
            while end < len(lines) and _LIST_LINE.match(lines[end]):
                end += 1
        elif _HEADING.match(line):
            end = index + 1
        else:
            end = index + 1
            while (
                end < len(lines)
                and lines[end].strip()
                and not _starts_block(lines[end])
            ):
                end += 1

        blocks.append("\n".join(lines[index:end]))
        index = end
    return blocks


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

    if page is None:
        _trace_read(
            trace, None, offset, limit, started, reason="invalid page"
        )
        return _error(_INVALID_PAGE_MESSAGE)

    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            page_response = await client.get(
                settings.base_url + f"/v1/pages/{page}"
            )
            if page_response.status_code >= 400:
                _trace_read(
                    trace,
                    page,
                    offset,
                    limit,
                    started,
                    reason=f"HTTP {page_response.status_code}",
                )
                return _error(_status_error(page_response.status_code))
            markdown_response = await client.get(
                settings.base_url + f"/v1/pages/{page}/markdown",
                params={"markdown_version": MARKDOWN_VERSION},
            )
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException:
        _trace_read(trace, page, offset, limit, started, reason="timeout")
        return _error(_TIMEOUT_MESSAGE)
    except httpx2.RequestError as exc:
        _trace_read(
            trace, page, offset, limit, started, reason=type(exc).__name__
        )
        return _error(_UNREACHABLE_MESSAGE)
    except Exception as exc:  # défense : le handler ne lève jamais
        _trace_read(
            trace, page, offset, limit, started, reason=type(exc).__name__
        )
        return _error(_UNREACHABLE_MESSAGE)

    if markdown_response.status_code >= 400:
        _trace_read(
            trace,
            page,
            offset,
            limit,
            started,
            reason=f"HTTP {markdown_response.status_code}",
        )
        return _error(_status_error(markdown_response.status_code))

    try:
        page_object = page_response.json()
        markdown_object = markdown_response.json()
        if (
            not isinstance(page_object, dict)
            or not isinstance(markdown_object, dict)
            or not isinstance(markdown_object.get("markdown"), str)
        ):
            raise ValueError("unexpected read body")
    except (TypeError, ValueError):
        _trace_read(
            trace, page, offset, limit, started, reason="unreadable body"
        )
        return _error(_UNREADABLE_MESSAGE)

    blocks = _markdown_blocks(markdown_object["markdown"])
    selected = blocks[offset : offset + limit]
    content = "\n\n".join(selected)
    next_offset = offset + len(selected)
    has_more = next_offset < len(blocks)
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
        "total_blocks": len(blocks),
        "truncated": truncated,
        "unknown_block_ids": unknown_ids,
        "query": None,
        "match_count": 0,
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
        blocks=len(selected),
        chars=len(content),
        total_blocks=len(blocks),
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
    blocks: int = 0,
    chars: int = 0,
    total_blocks: int = 0,
    truncated: bool = False,
    unknown_ids: list[object] | None = None,
    reason: str | None = None,
) -> None:
    """Trace une lecture : comptes, durée, statut ; jamais le contenu."""
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
    if reason is not None:
        entry["reason"] = reason
    trace("ReadPage", entry)


# -- Tool ``search_pages`` -----------------------------------------------------


def _tool_bindings(
    settings: _Settings, trace: Trace
) -> tuple[ToolBinding, ...]:
    """Les Tools du plugin ; les tickets suivants ajoutent une ligne ici."""
    return (
        _search_tool(settings, trace),
        _read_tool(settings, trace),
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
    """Corps exact de ``POST /v1/search`` (spec #111)."""
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
            response = await client.post(
                settings.base_url + "/v1/search", json=body
            )
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException:
        _trace_search(trace, query, started, reason="timeout")
        return _error(_TIMEOUT_MESSAGE)
    except httpx2.RequestError as exc:
        _trace_search(trace, query, started, reason=type(exc).__name__)
        return _error(_UNREACHABLE_MESSAGE)
    except Exception as exc:  # défense : le handler ne lève jamais
        _trace_search(trace, query, started, reason=type(exc).__name__)
        return _error(_UNREACHABLE_MESSAGE)

    if response.status_code >= 400:
        _trace_search(
            trace,
            query,
            started,
            reason=f"HTTP {response.status_code}",
        )
        return _error(_status_error(response.status_code))

    try:
        results, has_more, next_cursor = _search_page(response.json())
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
