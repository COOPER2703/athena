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


# -- Tool ``search_pages`` -----------------------------------------------------


def _tool_bindings(
    settings: _Settings, trace: Trace
) -> tuple[ToolBinding, ...]:
    """Les Tools de ce ticket ; les tickets suivants ajoutent une ligne ici."""
    return (
        _search_tool(settings, trace),
        _create_tool(settings, trace),
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
    "5 000 blocs ou limite de taille atteinte). Réessaie avec un contenu "
    "plus court ou plus simple."
)
_INVALID_PAGE_MESSAGE = (
    "Identifiant de page invalide : indique un identifiant Notion ou une URL "
    "de page."
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


def _create_body(
    parent: str, title: str, content: str
) -> dict[str, object]:
    """Corps exact de ``POST /v1/pages`` (spec #111).

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


def _created_summary(
    page: Mapping[str, Any], fallback_title: str
) -> dict[str, object]:
    """Projette la page créée ; le titre demandé sert de repli."""
    title = _page_title(page)
    if title == "(sans titre)":
        title = fallback_title
    return {
        "id": _text(page.get("id")),
        "title": title,
        "url": _text(page.get("url")),
    }


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
            response = await client.post(
                settings.base_url + "/v1/pages", json=body
            )
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException:
        _trace_create(
            trace,
            parent=parent,
            title=title,
            content=content,
            started=started,
            reason="timeout",
        )
        return _error(_TIMEOUT_MESSAGE)
    except httpx2.RequestError as exc:
        _trace_create(
            trace,
            parent=parent,
            title=title,
            content=content,
            started=started,
            reason=type(exc).__name__,
        )
        return _error(_UNREACHABLE_MESSAGE)
    except Exception as exc:  # défense : le handler ne lève jamais
        _trace_create(
            trace,
            parent=parent,
            title=title,
            content=content,
            started=started,
            reason=type(exc).__name__,
        )
        return _error(_UNREACHABLE_MESSAGE)

    if response.status_code >= 400:
        status = response.status_code
        _trace_create(
            trace,
            parent=parent,
            title=title,
            content=content,
            started=started,
            reason=f"HTTP {status}",
        )
        if status == 400:
            return _error(_CREATE_VALIDATION_MESSAGE)
        return _error(_status_error(status, write=True))

    try:
        page = response.json()
    except (TypeError, ValueError):
        page = None
    if not isinstance(page, dict):
        _trace_create(
            trace,
            parent=parent,
            title=title,
            content=content,
            started=started,
            reason="unreadable body",
        )
        return _error(_UNREADABLE_MESSAGE)

    payload = {
        "status": "ok",
        "page": _created_summary(page, title),
    }
    _trace_create(
        trace,
        parent=parent,
        title=title,
        content=content,
        started=started,
    )
    return payload


def _trace_create(
    trace: Trace,
    *,
    parent: str | None,
    title: str,
    content: str,
    started: float,
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
