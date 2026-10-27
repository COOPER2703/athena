"""Plugin ``websearch`` : recherche web par le moteur Tavily (ADR-0018).

Ce module ne connaît du serveur que le contrat public des plugins
(``plugin_api``) : il n'importe aucun interne. Il expose un seul Tool,
``web_search``, que Gemini appelle en Session pour obtenir des **Extraits de
recherche** plutôt que des pages entières.

Au démarrage, ``create`` sonde ``GET /usage`` : la sonde valide la clé sans
consommer de crédit. Une clé refusée ou un moteur injoignable lève
``PluginUnavailable`` — le chargeur trace ``failed``, démarre dégradé et
retente en tâche de fond (ADR-0014). Un appel de Tool, lui, ne lève jamais :
une panne devient une phrase rendue au modèle.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import httpx2

from plugin_api import (
    PluginBinding,
    PluginContext,
    PluginUnavailable,
    ToolBinding,
    ToolSpec,
)

# Valeurs sûres appliquées par le plugin quand la configuration manque : le
# chargeur ne sait pas porter de défaut (ADR-0018).
DEFAULT_BASE_URL = "https://api.tavily.com"
DEFAULT_SEARCH_DEPTH = "basic"
DEFAULT_LANGUAGE = "fr"
DEFAULT_COUNTRY = "france"
DEFAULT_STRICT_LANGUAGE = "0"

# Budgets HTTP (ADR-0018) : 8 s, un seul essai. Lus à chaque opération pour que
# les tests puissent les réduire sur une copie du module.
PROBE_TIMEOUT = 8.0
SEARCH_TIMEOUT = 8.0

# Budget de contexte (ADR-0018) : dix extraits, deux fragments par source.
MAX_RESULTS = 10
CHUNKS_PER_SOURCE = 2

# Credits qu'un appel consomme, par profondeur (ADR-0018).
CREDITS_PER_DEPTH = {"fast": 1, "basic": 1, "advanced": 2}

TOOL_NAME = "web_search"
TOOL_DESCRIPTION = (
    "Cherche sur le web et renvoie des extraits de pages sourcés "
    "(titre, URL, extrait, date de publication)."
)
TOOL_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "La question ou les mots-clés à chercher sur le web",
        },
    },
    "required": ["query"],
}

INSTRUCTION = (
    "`web_search` interroge le web. Utilise-le pour toute question factuelle, "
    "d'actualité ou de culture générale dont tu n'es pas sûre, et jamais pour "
    "une question sur l'utilisateur ou sa maison.\n"
    "Avant d'appeler, dis en une phrase courte que tu cherches. Un appel suffit : "
    "ne relance pas la même question reformulée.\n"
    "Quand tu réponds, attribue à la source par son nom ; ne lis jamais une URL "
    "à haute voix et ne récite pas d'extrait brut.\n"
    "Si les sources se contredisent, dis-le au lieu de trancher. Si la recherche "
    "échoue, dis-le simplement et propose de réessayer."
)

_UNREACHABLE_MESSAGE = (
    "La recherche web n'a pas abouti : le moteur est injoignable. "
    "Réessaie dans un instant."
)
_TIMEOUT_MESSAGE = (
    "La recherche web n'a pas abouti : le moteur n'a pas répondu à temps. "
    "Réessaie dans un instant."
)
_REFUSED_MESSAGE = (
    "La recherche web a été refusée par le moteur. "
    "Vérifie la clé d'API, puis réessaie."
)

Trace = Callable[..., None]


@dataclass(frozen=True, slots=True)
class _Settings:
    """Cible et réglages du plugin, secrets compris (la clé n'est jamais tracée)."""

    base_url: str
    api_key: str
    search_depth: str
    language: str
    country: str
    strict_language: bool


async def create(context: PluginContext) -> PluginBinding:
    """Sonde le moteur, puis rend le Tool ``web_search`` et son instruction.

    La sonde est la seule opération qui peut lever ``PluginUnavailable`` : une
    clé refusée ou un moteur injoignable est une panne réparable, retentée par
    le chargeur. Un ``/usage`` en erreur inattendue ne bloque pas le démarrage.
    """
    settings = _settings(context)
    await _probe(settings, context.trace)
    return PluginBinding(name=context.name, tools=(), instruction=INSTRUCTION)


def _settings(context: PluginContext) -> _Settings:
    """Applique les défauts sur la configuration déclarée par le manifeste."""
    config = context.config

    def text(key: str, default: str) -> str:
        return str(config.get(key, "")).strip() or default

    return _Settings(
        base_url=text("base_url", DEFAULT_BASE_URL).rstrip("/"),
        api_key=str(context.secrets.get("api_key", "")),
        search_depth=text("search_depth", DEFAULT_SEARCH_DEPTH),
        language=text("language", DEFAULT_LANGUAGE),
        country=text("country", DEFAULT_COUNTRY),
        strict_language=_as_flag(text("strict_language", DEFAULT_STRICT_LANGUAGE)),
    )


def _as_flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


async def _probe(settings: _Settings, trace: Trace) -> None:
    """``GET /usage`` : valide la clé sans consommer de crédit.

    Seules l'authentification refusée et l'injoignabilité sont des pannes
    retentables. Un 5xx inattendu est tracé puis ignoré : le moteur répond,
    c'est sa sonde qui est en défaut, pas le plugin.
    """
    checklist = _client(settings, PROBE_TIMEOUT)
    async with checklist as client:
        try:
            response = await client.get(settings.base_url + "/usage")
        except asyncio.CancelledError:
            raise
        except httpx2.TimeoutException as exc:
            trace("ProbeFailed", {"reason": "timeout"}, level="warning")
            raise PluginUnavailable(
                "websearch probe failed: the engine did not answer in time"
            ) from exc
        except httpx2.RequestError as exc:
            trace(
                "ProbeFailed",
                {"reason": type(exc).__name__},
                level="warning",
            )
            raise PluginUnavailable(
                f"websearch probe failed: the engine is unreachable "
                f"({type(exc).__name__})"
            ) from exc

    if response.status_code in (401, 403):
        trace("ProbeFailed", {"status": response.status_code}, level="warning")
        raise PluginUnavailable(
            f"websearch probe failed: the engine refused the API key "
            f"(HTTP {response.status_code})"
        )
    if response.status_code >= 500:
        trace(
            "ProbeUnavailable",
            {"status": response.status_code},
            level="warning",
        )
        return
    _trace_credits(_usage_body(response), trace)


def _client(settings: _Settings, timeout: float) -> httpx2.AsyncClient:
    """Client HTTP porteur du Bearer ; la clé ne quitte jamais cette en-tête."""
    return httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {settings.api_key}"},
        timeout=httpx2.Timeout(timeout),
    )


def _usage_body(response: httpx2.Response) -> Mapping[str, Any]:
    """Lit le corps ``/usage`` sans jamais lever sur un JSON illisible."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _trace_credits(body: Mapping[str, Any], trace: Trace) -> None:
    """Trace ``usage``/``limit`` et avertit quand le quota est atteint.

    Tavily expose les compteurs à la racine ou sous ``key`` selon les versions :
    les deux formes sont lues, sans jamais toucher à la clé.
    """
    counters: Mapping[str, Any] = body
    nested = body.get("key")
    if isinstance(nested, dict) and (
        "usage" not in counters or "limit" not in counters
    ):
        counters = nested
    usage = _as_int(counters.get("usage"))
    limit = _as_int(counters.get("limit"))
    if usage is None or limit is None:
        trace("CreditsUnknown", {"reason": "usage/limit absent"}, level="debug")
        return
    remaining = max(limit - usage, 0)
    payload = {"usage": usage, "limit": limit, "remaining": remaining}
    trace("CreditsRemaining", payload)
    if remaining == 0:
        trace("CreditsExhausted", payload, level="warning")


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
