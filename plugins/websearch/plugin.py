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
import re
from collections import OrderedDict
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
# Un extrait ne dépasse pas 600 caractères, l'ensemble 6 000 (ADR-0018).
MAX_EXCERPT_CHARS = 600
MAX_EXCERPT_TOTAL = 6000

# Credits qu'un appel consomme, par profondeur (ADR-0018).
CREDITS_PER_DEPTH = {"fast": 1, "basic": 1, "advanced": 2}

# Cache des recherches (ADR-0018) : en mémoire seulement, borné en taille et en
# âge. Lus à chaque opération pour que les tests puissent les réduire sur une
# copie du module.
CACHE_MAX_ENTRIES = 32
CACHE_TTL = 300.0


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Tout ce qui distingue deux recherches pour le moteur (ADR-0018).

    Une dimension oubliée ici rendrait à une recherche la charge d'une autre :
    la requête normalisée, le topic et la fenêtre viennent du modèle ; la
    langue, la profondeur, le pays et le filtre strict, de la configuration.
    """

    query: str
    topic: str
    time_range: str | None
    language: str
    search_depth: str
    country: str
    strict_language: bool


TOOL_NAME = "web_search"
TOOL_DESCRIPTION = (
    "Cherche sur le web et renvoie des extraits de recherche sourcés "
    "(titre, URL, extrait, date de publication)."
)

# Énumérations offertes au modèle (ADR-0018) : le schéma est passé tel quel à
# Gemini, c'est donc la seule validation côté modèle.
TOPICS = ("general", "news", "finance")
TIME_RANGES = ("day", "week", "month", "year")
DEFAULT_TOPIC = "general"

TOOL_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "La question ou les mots-clés à chercher sur le web",
        },
        "topic": {
            "type": "string",
            "enum": list(TOPICS),
            "description": (
                "Le type de recherche : `general` pour une question de culture "
                "générale, `news` pour l'actualité, `finance` pour la finance"
            ),
        },
        "time_range": {
            "type": "string",
            "enum": list(TIME_RANGES),
            "description": (
                "La fenêtre de fraîcheur demandée par la question : `day`, "
                "`week`, `month` ou `year`"
            ),
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
    "échoue, dis-le simplement et propose de réessayer.\n"
    "Si les extraits ne contiennent pas la réponse, dis que tu ne sais pas ; si la "
    "recherche ne rend aucun résultat, dis que tu n'as rien trouvé, sans rien "
    "inventer."
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
_UNREADABLE_MESSAGE = (
    "La recherche web a échoué : le moteur a renvoyé une réponse illisible. "
    "Réessaie dans un instant."
)
_STATUS_MESSAGE = (
    "La recherche web a échoué (réponse {status} du moteur). "
    "Réessaie dans un instant."
)

Trace = Callable[..., None]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """Une charge de recherche réussie et l'instant où elle a été obtenue."""

    payload: Mapping[str, Any]
    stored_at: float


# Le cache vit avec le module : une retente de ``create()`` (moteur réparé) ne
# jette pas les recherches déjà payées. Aucune persistance, aucun disque.
_CACHE: OrderedDict[CacheKey, _CacheEntry] = OrderedDict()


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
    return PluginBinding(
        name=context.name,
        tools=(_tool(settings, context.trace),),
        instruction=INSTRUCTION,
    )


def _settings(context: PluginContext) -> _Settings:
    """Applique les défauts sur la configuration déclarée par le manifeste."""
    config = context.config

    def text(key: str, default: str) -> str:
        return str(config.get(key, "")).strip() or default

    return _Settings(
        base_url=text("base_url", DEFAULT_BASE_URL).rstrip("/"),
        api_key=str(context.secrets.get("api_key", "")),
        search_depth=_depth(text("search_depth", DEFAULT_SEARCH_DEPTH)),
        language=text("language", DEFAULT_LANGUAGE),
        country=text("country", DEFAULT_COUNTRY),
        strict_language=_as_flag(text("strict_language", DEFAULT_STRICT_LANGUAGE)),
    )


def _depth(value: str) -> str:
    """Retient une profondeur que le moteur connaît, sinon ``basic``.

    Les clés de ``CREDITS_PER_DEPTH`` sont les seules valeurs acceptées par
    Tavily ; une faute de frappe dans ``plugins.json`` ne doit pas partir au
    moteur ni fausser le compte de crédits.
    """
    return value if value in CREDITS_PER_DEPTH else DEFAULT_SEARCH_DEPTH


def _as_flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


async def _probe(settings: _Settings, trace: Trace) -> None:
    """``GET /usage`` : valide la clé sans consommer de crédit.

    Seules l'authentification refusée et l'injoignabilité sont des pannes
    retentables. Un 5xx inattendu est tracé puis ignoré : le moteur répond,
    c'est sa sonde qui est en défaut, pas le plugin.
    """
    async with _client(settings, PROBE_TIMEOUT) as client:
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
        # Le moteur répond : sa sonde est en défaut, le plugin ne l'est pas.
        trace(
            "ProbeDegraded",
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


# -- Tool ``web_search`` -------------------------------------------------------


def _tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare le Tool et son handler : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _search(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            parameters=TOOL_PARAMETERS,
        ),
        handler=handle,
    )


def _choice(value: object, allowed: tuple[str, ...]) -> str | None:
    """Retient une valeur d'énumération ; tout le reste est ignoré."""
    return value if isinstance(value, str) and value in allowed else None


def _request_body(
    settings: _Settings,
    query: str,
    topic: str,
    time_range: str | None,
) -> dict[str, object]:
    """Corps de ``POST /search``, borné par le budget de contexte (ADR-0018).

    ``topic`` vient du modèle (``general`` par défaut) et ``time_range`` n'est
    envoyé que s'il est fourni. ``country`` n'accompagne que ``general`` :
    Tavily répond ``400`` pour un topic d'actualité. ``filter_by_language``
    traduit ``strict_language``.
    """
    body: dict[str, object] = {
        "query": query,
        "topic": topic,
        "max_results": MAX_RESULTS,
        "chunks_per_source": CHUNKS_PER_SOURCE,
        "search_depth": settings.search_depth,
        "language": settings.language,
        "filter_by_language": settings.strict_language,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    if time_range is not None:
        body["time_range"] = time_range
    if topic == DEFAULT_TOPIC:
        body["country"] = settings.country
    return body


# Ponctuation finale ignorée par la clé de cache : « c'est qui ? » et
# « c'est qui » sont la même question.
_TRAILING_PUNCTUATION = re.compile(r"[\s.!?,;:…]+$")


def _normalize_query(query: str) -> str:
    """Casse, espaces et ponctuation finale ne distinguent pas deux requêtes."""
    collapsed = " ".join(query.split()).casefold()
    return _TRAILING_PUNCTUATION.sub("", collapsed)


def _cache_key(
    settings: _Settings,
    query: str,
    topic: str,
    time_range: str | None,
) -> CacheKey:
    """Deux recherches ne se confondent que si tous leurs paramètres concordent."""
    return CacheKey(
        query=_normalize_query(query),
        topic=topic,
        time_range=time_range,
        language=settings.language,
        search_depth=settings.search_depth,
        country=settings.country,
        strict_language=settings.strict_language,
    )


def _cache_get(key: CacheKey, now: float) -> Mapping[str, Any] | None:
    """Rend la charge encore fraîche, ou l'oublie si son TTL est passé."""
    entry = _CACHE.get(key)
    if entry is None:
        return None
    if now - entry.stored_at >= CACHE_TTL:
        del _CACHE[key]
        return None
    _CACHE.move_to_end(key)
    return entry.payload


def _cache_store(key: CacheKey, payload: Mapping[str, Any], now: float) -> None:
    """Mémorise une recherche réussie et jette la plus ancienne au-delà du plafond."""
    if CACHE_MAX_ENTRIES <= 0:
        return
    _CACHE[key] = _CacheEntry(payload=payload, stored_at=now)
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)


async def _search(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Interroge le moteur et rend des Extraits de recherche, ou une erreur.

    Une question déjà posée et encore fraîche rend la même charge sans requête
    HTTP : la Console voit ``hit`` dans la Trace et zéro crédit consommé. Une
    erreur n'entre jamais au cache, sans quoi une panne passagère collerait à
    la question. Aucune exception ne remonte : une panne devient une phrase en
    français que le modèle peut dire à haute voix (ADR-0018). Un timeout n'est
    pas retenté : une reprise doublerait la latence d'une conversation en
    cours.
    """
    query = str(arguments.get("query", "")).strip()
    topic = _choice(arguments.get("topic"), TOPICS) or DEFAULT_TOPIC
    time_range = _choice(arguments.get("time_range"), TIME_RANGES)
    payload = _request_body(settings, query, topic, time_range)
    key = _cache_key(settings, query, topic, time_range)
    loop = asyncio.get_running_loop()
    started = loop.time()
    cached = _cache_get(key, started)
    if cached is not None:
        _trace_search(
            trace,
            query,
            payload,
            started,
            results=len(cached["results"]),
            cache="hit",
        )
        return cached
    try:
        async with _client(settings, SEARCH_TIMEOUT) as client:
            response = await client.post(settings.base_url + "/search", json=payload)
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException:
        _trace_search(trace, query, payload, started, reason="timeout")
        return _error(_TIMEOUT_MESSAGE)
    except httpx2.RequestError as exc:
        _trace_search(trace, query, payload, started, reason=type(exc).__name__)
        return _error(_UNREACHABLE_MESSAGE)
    except Exception as exc:  # défense : le handler ne lève jamais
        _trace_search(trace, query, payload, started, reason=type(exc).__name__)
        return _error(_UNREACHABLE_MESSAGE)

    if response.status_code in (401, 403):
        _trace_search(
            trace, query, payload, started, reason=f"HTTP {response.status_code}"
        )
        return _error(_REFUSED_MESSAGE)
    if response.status_code >= 400:
        _trace_search(
            trace, query, payload, started, reason=f"HTTP {response.status_code}"
        )
        return _error(_STATUS_MESSAGE.format(status=response.status_code))

    try:
        results = _extracts(response.json())
    except (TypeError, ValueError):
        _trace_search(trace, query, payload, started, reason="unreadable body")
        return _error(_UNREADABLE_MESSAGE)

    response_payload = {"status": "ok", "count": len(results), "results": results}
    _cache_store(key, response_payload, loop.time())
    _trace_search(trace, query, payload, started, results=len(results), cache="miss")
    return response_payload


# Terminateurs de phrase et caractères qui peuvent les suivre (guillemets).
_SENTENCE_ENDS = ".!?…"
_SENTENCE_TAIL = " \n\t\"'»”)"


def _cut_excerpt(text: str, limit: int) -> tuple[str, bool]:
    """Coupe ``text`` à ``limit`` en reculant vers la fin propre la plus proche.

    Trois paliers, du plus lisible au plus brutal : fin de phrase, puis fin de
    mot, puis coupe nette. Le second membre dit si l'extrait est incomplet.
    """
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    for index in range(len(window) - 1, -1, -1):
        if window[index] not in _SENTENCE_ENDS:
            continue
        if index + 1 == len(window) or window[index + 1] in _SENTENCE_TAIL:
            return window[: index + 1].rstrip(), True
    for index in range(len(window) - 1, 0, -1):
        if window[index].isspace():
            return window[:index].rstrip(), True
    return window.rstrip(), True


def _extracts(body: object) -> list[dict[str, object]]:
    """Projette la réponse du moteur sur le contrat du Tool (glossaire).

    Une réponse sans liste ``results`` est *illisible*, pas vide : la confondre
    avec zéro résultat ferait passer une panne pour une absence d'information.
    ``published_date`` n'apparaît que si le moteur la fournit ; l'absence de
    date ne fait jamais disparaître un résultat. Dix résultats au plus
    nourrissent la réponse, dans l'ordre du moteur. Un extrait coupé porte
    ``truncated`` : le modèle sait que le texte n'est pas complet, et un
    résultat intact — ou laissé sans extrait — ne porte jamais de faux
    indicateur.
    """
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ValueError("unexpected search body")
    raw = body["results"]
    extracts: list[dict[str, object]] = []
    remaining = MAX_EXCERPT_TOTAL
    for item in raw:
        if len(extracts) >= MAX_RESULTS:
            break
        if not isinstance(item, dict):
            continue
        # Le budget total se partage dans l'ordre du moteur : un résultat
        # au-delà du plafond garde son titre et son URL, sans extrait.
        excerpt, truncated = _cut_excerpt(
            str(item.get("content") or ""),
            min(MAX_EXCERPT_CHARS, max(remaining, 0)),
        )
        remaining -= len(excerpt)
        extract: dict[str, object] = {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "excerpt": excerpt,
        }
        if truncated and excerpt:
            extract["truncated"] = True
        published = item.get("published_date")
        if isinstance(published, str) and published.strip():
            extract["published_date"] = published.strip()
        extracts.append(extract)
    return extracts


# Paramètres tracés dans la Console : de quoi diagnostiquer une recherche
# hors sujet ou vide, sans jamais la clé.
_TRACED_PARAMS = (
    "topic",
    "time_range",
    "max_results",
    "chunks_per_source",
    "search_depth",
    "language",
    "country",
)


def _trace_search(
    trace: Trace,
    query: str,
    payload: Mapping[str, object],
    started: float,
    *,
    results: int = 0,
    reason: str | None = None,
    cache: str = "miss",
) -> None:
    """Trace une recherche : requête, paramètres, durée, résultats, cache, crédits."""
    entry: dict[str, object] = {
        "query": query,
        # Les paramètres effectivement partis au moteur : ni plus, ni moins.
        "params": {
            key: payload[key]
            for key in _TRACED_PARAMS
            if key in payload
        },
        "duration_ms": round((asyncio.get_running_loop().time() - started) * 1000, 1),
        "results": results,
        "cache": cache,
        # Un hit et une erreur ne consomment aucun crédit.
        "credits": 0 if reason is not None or cache == "hit" else _credits(payload),
    }
    if reason is not None:
        entry["status"] = "error"
        entry["reason"] = reason
    trace("Search", entry)


def _credits(payload: Mapping[str, object]) -> int:
    depth = str(payload.get("search_depth", DEFAULT_SEARCH_DEPTH))
    return CREDITS_PER_DEPTH.get(depth, 1)


def _error(message: str) -> Mapping[str, object]:
    return {"status": "error", "message": message}
