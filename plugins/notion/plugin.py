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

_UPDATE_DESCRIPTION = (
    "ajoute du contenu à une page, remplace son corps sur confirmation, "
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
    "Le remplacement du contenu exige une confirmation explicite et n'est "
    "pas effectué par cette version du plugin. Utilise le mode append pour "
    "ajouter du contenu."
)
_NO_TITLE_PROPERTY_MESSAGE = (
    "La page n'expose pas de propriété de titre : le renommage est impossible."
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


def _page_brief(page: Mapping[str, Any]) -> dict[str, object]:
    """La forme ``page`` rendue au modèle : identifiant, titre, URL."""
    summary = _page_summary(page)
    return {
        "id": summary["id"],
        "title": summary["title"],
        "url": summary["url"],
    }


def _merge_page(
    base: dict[str, object], page: object
) -> dict[str, object]:
    """Titre et URL rendus par un renommage, sans écraser par du vide."""
    if not isinstance(page, dict):
        return base
    summary = dict(base)
    if _text(page.get("id")):
        summary["id"] = _text(page.get("id"))
    if _title_key(page) is not None:
        summary["title"] = _page_title(page)
    if _text(page.get("url")):
        summary["url"] = _text(page.get("url"))
    return summary


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


async def _patch(
    settings: _Settings, path: str, body: Mapping[str, object]
) -> tuple[int | None, object, str | None]:
    """Un PATCH d'écriture, un seul essai : ``(statut, corps, raison)``.

    Une panne de transport rend ``status=None`` et une raison ; un statut
    d'erreur rend la raison ``HTTP n``. Le handler décide ensuite de la phrase.
    """
    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            response = await client.patch(settings.base_url + path, json=body)
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException:
        return None, None, "timeout"
    except httpx2.RequestError as exc:
        return None, None, type(exc).__name__
    except Exception as exc:  # défense : le handler ne lève jamais
        return None, None, type(exc).__name__

    if response.status_code >= 400:
        return response.status_code, None, f"HTTP {response.status_code}"
    try:
        parsed = response.json()
    except (TypeError, ValueError):
        return response.status_code, None, None
    return response.status_code, parsed, None


def _write_message(status: int | None, reason: str) -> str:
    """Phrase française d'une écriture échouée, incertaine sur 503/504."""
    if status is not None:
        return _status_error(status, write=True)
    if reason == "timeout":
        return _TIMEOUT_MESSAGE
    return _UNREACHABLE_MESSAGE


def _partial_message(
    title_failure: str | None, content_failure: str | None
) -> str:
    """Phrase d'un succès partiel : ce qui a réussi, ce qui a échoué."""
    if title_failure and content_failure:
        message = (
            "Ni le titre ni le contenu n'ont pu être mis à jour. "
            f"{title_failure} {content_failure}"
        )
    elif title_failure:
        message = (
            "Le contenu a été ajouté, mais le titre n'a pas pu être "
            f"modifié. {title_failure}"
        )
    else:
        message = (
            "Le titre a été mis à jour, mais le contenu n'a pas pu être "
            f"ajouté. {content_failure}"
        )
    if "relis" not in message.lower():
        message += " Relis la page avant de réessayer."
    return message


async def _update(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Ajoute du contenu, renomme, et dit clairement un succès partiel.

    La page est lue d'abord pour découvrir la clé de sa propriété de titre et
    rendre son identité. Le renommage et l'ajout sont deux écritures séparées,
    tentées indépendamment ; aucune n'est rejouée. Le remplacement appartient
    au ticket #117 : ``mode=replace`` est refusé sans aucune écriture.
    """
    started = asyncio.get_running_loop().time()
    page_ref = _page_id(arguments.get("page"))
    if page_ref is None:
        _trace_update(trace, None, "append", started, reason="invalid page")
        return _error(_INVALID_PAGE_MESSAGE)

    mode = _update_mode(arguments.get("mode"))
    if mode == "replace":
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

    try:
        async with _client(settings, REQUEST_TIMEOUT) as client:
            response = await client.get(
                settings.base_url + f"/v1/pages/{page_ref}"
            )
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException:
        _trace_update(trace, page_ref, mode, started, reason="timeout")
        return _error(_TIMEOUT_MESSAGE)
    except httpx2.RequestError as exc:
        _trace_update(
            trace, page_ref, mode, started, reason=type(exc).__name__
        )
        return _error(_UNREACHABLE_MESSAGE)
    except Exception as exc:  # défense : le handler ne lève jamais
        _trace_update(
            trace, page_ref, mode, started, reason=type(exc).__name__
        )
        return _error(_UNREACHABLE_MESSAGE)

    if response.status_code >= 400:
        _trace_update(
            trace,
            page_ref,
            mode,
            started,
            reason=f"HTTP {response.status_code}",
        )
        return _error(_status_error(response.status_code))

    try:
        page = response.json()
        if not isinstance(page, dict):
            raise ValueError("unexpected page body")
    except (TypeError, ValueError):
        _trace_update(
            trace, page_ref, mode, started, reason="unreadable body"
        )
        return _error(_UNREADABLE_MESSAGE)

    summary = _page_brief(page)
    key = _title_key(page)
    title_updated = False
    content_updated = False
    title_failure: str | None = None
    content_failure: str | None = None
    title_reason: str | None = None
    content_reason: str | None = None
    warnings: list[object] = []

    if title:
        if key is None:
            title_failure = _NO_TITLE_PROPERTY_MESSAGE
            title_reason = "no title property"
        else:
            status, result, reason = await _patch(
                settings,
                f"/v1/pages/{page_ref}",
                _rename_body(key, title),
            )
            if reason is None:
                title_updated = True
                summary = _merge_page(summary, result)
            else:
                title_failure = _write_message(status, reason)
                title_reason = reason

    if content:
        status, result, reason = await _patch(
            settings,
            f"/v1/pages/{page_ref}/markdown",
            _append_body(content),
        )
        if reason is None:
            content_updated = True
            if isinstance(result, dict) and isinstance(
                result.get("warnings"), list
            ):
                warnings = result["warnings"]
        else:
            content_failure = _write_message(status, reason)
            content_reason = reason

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
            warnings=len(warnings),
            reason=reason or "partial",
        )
        return {
            "status": "error",
            "message": _partial_message(title_failure, content_failure),
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
            warnings=len(warnings),
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
        warnings=len(warnings),
    )
    return {"status": "ok", **flags}


def _trace_update(
    trace: Trace,
    page: str | None,
    mode: str,
    started: float,
    *,
    title_updated: bool = False,
    content_updated: bool = False,
    chars: int = 0,
    warnings: int = 0,
    reason: str | None = None,
) -> None:
    """Trace un ``update_page`` : jamais le contenu, jamais le jeton."""
    entry: dict[str, object] = {
        "page": page,
        "mode": mode,
        "title_updated": title_updated,
        "content_updated": content_updated,
        "chars": chars,
        "warnings": warnings,
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
    """Échec d'un appel : porte la phrase française et le motif de la Trace."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(reason)
        self.message = message
        self.reason = reason


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


def _page_reference(
    page_body: Mapping[str, Any], page: str
) -> dict[str, object]:
    """La forme ``page`` rendue au modèle, l'identifiant résolu en secours."""
    reference = _page_brief(page_body)
    if not reference["id"]:
        reference["id"] = page
    return reference


async def _notion_call(
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
    deviennent tous une phrase française ; aucun essai n'est rejoué.
    """
    try:
        response = await client.request(method, url, params=params, json=body)
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException as exc:
        raise _ActionFailed(_TIMEOUT_MESSAGE, "timeout") from exc
    except httpx2.RequestError as exc:
        raise _ActionFailed(_UNREACHABLE_MESSAGE, type(exc).__name__) from exc

    if response.status_code >= 400:
        raise _ActionFailed(
            _status_error(response.status_code, write=write),
            f"HTTP {response.status_code}",
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
    return await _notion_call(
        client, "GET", settings.base_url + f"/v1/pages/{page}"
    )


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
        body = await _notion_call(client, "GET", url, params=params)
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
            reference = _page_reference(page_body, page)
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
    except _ActionFailed as failure:
        _trace_trash(
            trace,
            page=page,
            started=started,
            child_pages=child_pages,
            reason=failure.reason,
        )
        return _error(failure.message)

    status, _, reason = await _patch(
        settings, f"/v1/pages/{page}", {"in_trash": True}
    )
    if reason is not None:
        _trace_trash(
            trace,
            page=page,
            started=started,
            child_pages=child_pages,
            reason=reason,
        )
        return _error(_write_message(status, reason))

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

    status, body, reason = await _patch(
        settings, f"/v1/pages/{page}", {"in_trash": False}
    )
    if reason is not None:
        _trace_restore(
            trace, page=page, started=started, reason=reason
        )
        return _error(_write_message(status, reason))

    if (
        isinstance(body, dict)
        and _text(body.get("url"))
        and isinstance(body.get("properties"), dict)
    ):
        reference = _page_reference(body, page)
    else:
        # Le corps de l'écriture ne portait pas la page : on la relit.
        try:
            async with _client(settings, REQUEST_TIMEOUT) as client:
                page_body = await _page_object(client, settings, page)
        except _ActionFailed as failure:
            _trace_restore(
                trace, page=page, started=started, reason=failure.reason
            )
            return _error(failure.message)
        reference = _page_reference(page_body, page)

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
