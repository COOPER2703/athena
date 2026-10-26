"""Plugin ``homeassistant`` : client du Serveur MCP HA (ADR-0015).

Athena ne parle à Home Assistant que par l'endpoint MCP **Assist**
(``/api/mcp/assist``), en Streamable HTTP stateless avec un token longue durée.
Ce module ne connaît du serveur que les types du contrat de plugin
(``PluginContext``, ``PluginBinding``) : il n'importe aucun interne.

Le serveur HA est *stateless* : chaque opération (découverte, instruction,
appel de Tool) ouvre sa propre session MCP. Rien n'est donc à surveiller, et un
redémarrage de Home Assistant se répare tout seul à l'appel suivant, sans
redémarrer Athena.

Délais (ADR-0015) : 5 s pour la découverte et l'instruction, 10 s pour un
appel. Ce module ne lève jamais vers le noyau (sauf annulation) : une panne de
la maison devient un résultat d'erreur rendu au modèle.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import PaginatedRequestParams

from server.core.plugins import PluginBinding, PluginContext, ToolBinding
from server.core.ports import ToolSpec

# Endpoint de l'API Assist : seul endpoint MCP qui ne demande pas de compte
# administrateur et dont le périmètre est exactement l'Exposition Assist.
ASSIST_PATH = "/api/mcp/assist"

# Budgets de l'ADR-0015. Lus à chaque opération (jamais figés au chargement)
# pour que les tests puissent les réduire sans toucher aux valeurs de prod.
DISCOVERY_TIMEOUT = 5.0
CALL_TIMEOUT = 10.0

# Nom du prompt Assist, d'après `AssistAPI.name` dans Home Assistant. Ce n'est
# qu'un repli : le nom réel est découvert par ``prompts/list``.
EXPECTED_PROMPT_NAME = "Device control"

_TIMEOUT_MESSAGE = "Home Assistant n'a pas répondu à temps"
_UNREACHABLE_MESSAGE = "Home Assistant est injoignable"
_REFUSED_MESSAGE = "Home Assistant a refusé la demande"

Trace = Callable[..., None]


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """Cible MCP du plugin : l'URL complète et le token (jamais tracé)."""

    url: str
    token: str


async def create(context: PluginContext) -> PluginBinding:
    """Point d'entrée du plugin : découvre les Tools Assist, rend le binding.

    Asynchrone : la découverte est une opération d'ouverture, exécutée dans la
    boucle du serveur (une seule boucle, ADR-0005).
    """
    endpoint = _endpoint(context)
    tools, instruction = await _discover(endpoint, context.trace)
    context.trace(
        "PluginReady",
        {"tools": len(tools), "instruction_chars": len(instruction)},
    )
    return PluginBinding(
        name=context.name,
        tools=tools,
        instruction=instruction,
    )


def _endpoint(context: PluginContext) -> _Endpoint:
    """Construit l'URL Assist à partir de l'URL non secrète de l'instance."""
    base = str(context.config.get("url", "")).strip().rstrip("/")
    return _Endpoint(
        url=base + ASSIST_PATH,
        token=str(context.secrets.get("token", "")),
    )


# -- Découverte et instruction -------------------------------------------------


async def _discover(
    endpoint: _Endpoint, trace: Trace
) -> tuple[tuple[ToolBinding, ...], str]:
    """Découvre les Tools et l'Instruction de plugin, sans jamais lever.

    Ouverture de session, ``tools/list`` et instruction tiennent dans le même
    budget ``DISCOVERY_TIMEOUT``. L'instruction ne reçoit que le temps restant,
    et son échec est absorbé : une panne de ``prompts/get`` ne prive jamais le
    modèle des Tools déjà découverts (ADR-0015).
    """
    discovered: list[Any] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DISCOVERY_TIMEOUT
    try:
        async with contextlib.AsyncExitStack() as stack:
            async with asyncio.timeout_at(deadline):
                session = await stack.enter_async_context(
                    _mcp_session(endpoint, timeout=DISCOVERY_TIMEOUT)
                )
                discovered = await _list_tools(session)
            trace("ToolsDiscovered", {"count": len(discovered)})
            for tool in discovered:
                _trace_annotations(trace, tool)
            # Le garde extérieur est refermé : plus de course entre deux délais.
            instruction = await _instruction_or_empty(
                session, trace, deadline - loop.time()
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Un groupe d'exceptions du transport (anyio) est un ``Exception`` :
        # cette branche couvre donc aussi une maison éteinte ou un 401.
        trace("DiscoveryFailed", {"error": _describe(exc)}, level="warning")
        return (), ""
    return (
        tuple(_tool_binding(endpoint, tool, trace) for tool in discovered),
        instruction,
    )


async def _list_tools(session: ClientSession) -> list[Any]:
    """Lit ``tools/list``, en suivant le curseur s'il y en a un."""
    tools: list[Any] = []
    cursor: str | None = None
    while True:
        params = PaginatedRequestParams(cursor=cursor) if cursor else None
        page = await session.list_tools(params=params)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools


def _trace_annotations(trace: Trace, tool: Any) -> None:
    """Trace les annotations MCP d'un Tool ; elles ne filtrent rien (ADR-0015)."""
    annotations = tool.annotations
    trace(
        "ToolAnnotations",
        {
            "tool": tool.name,
            "read_only": None if annotations is None else annotations.read_only_hint,
            "destructive": (
                None if annotations is None else annotations.destructive_hint
            ),
            "idempotent": (
                None if annotations is None else annotations.idempotent_hint
            ),
            "open_world": (
                None if annotations is None else annotations.open_world_hint
            ),
        },
        level="debug",
    )


async def _instruction_or_empty(
    session: ClientSession, trace: Trace, budget: float
) -> str:
    """Récupère l'Instruction de plugin dans le temps restant de la découverte."""
    if budget <= 0:
        trace(
            "InstructionUnavailable",
            {"reason": "discovery budget exhausted"},
            level="warning",
        )
        return ""
    try:
        async with asyncio.timeout(budget):
            return await _fetch_instruction(session, trace)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        trace(
            "InstructionUnavailable",
            {"error": _describe(exc)},
            level="warning",
        )
        return ""


async def _fetch_instruction(session: ClientSession, trace: Trace) -> str:
    """Découvre le prompt Assist par ``prompts/list`` puis le lit tel quel."""
    prompts = (await session.list_prompts()).prompts
    name = _prompt_name(prompts)
    if name is None:
        trace("InstructionUnavailable", {"reason": "no matching prompt"})
        return ""
    for candidate in dict.fromkeys((name, EXPECTED_PROMPT_NAME)):
        try:
            result = await session.get_prompt(candidate)
        except MCPError as exc:
            # Un prompt renommé par HA arrive ici (code 0, « Unknown prompt »).
            trace(
                "InstructionUnavailable",
                {"name": candidate, "code": exc.code},
                level="debug",
            )
            continue
        text = _prompt_text(result)
        if text:
            trace("InstructionLoaded", {"name": candidate, "chars": len(text)})
            return text
        trace("InstructionUnavailable", {"name": candidate, "reason": "empty"})
        return ""
    return ""


def _prompt_name(prompts: list[Any]) -> str | None:
    """Choisit le prompt Assist sans dépendre de son nom exact."""
    if not prompts:
        return None
    for prompt in prompts:
        if prompt.name == EXPECTED_PROMPT_NAME:
            return prompt.name
    for prompt in prompts:
        description = (prompt.description or "").lower()
        if EXPECTED_PROMPT_NAME.lower() in description:
            return prompt.name
    # HA n'expose qu'un prompt sur cet endpoint : un nom unique est le sien.
    return prompts[0].name if len(prompts) == 1 else None


def _prompt_text(result: Any) -> str:
    """Aplatit tous les blocs texte, quel que soit le rôle du message."""
    return "\n\n".join(
        message.content.text
        for message in result.messages
        if message.content.type == "text" and message.content.text.strip()
    )


# -- Appel d'un Tool -----------------------------------------------------------


def _tool_binding(endpoint: _Endpoint, tool: Any, trace: Trace) -> ToolBinding:
    """Expose un Tool Assist tel quel : même nom, description et schéma."""
    name = tool.name

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _call_tool(endpoint, name, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name=name,
            description=tool.description or "",
            parameters=tool.input_schema,
        ),
        handler=handle,
    )


async def _call_tool(
    endpoint: _Endpoint,
    name: str,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Appelle ``tools/call`` dans une session neuve ; ne lève jamais."""
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            async with _mcp_session(endpoint, timeout=CALL_TIMEOUT) as session:
                result = await session.call_tool(
                    name,
                    dict(arguments),
                    read_timeout_seconds=CALL_TIMEOUT,
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = _failure_message(exc)
        trace("ToolFailed", {"tool": name, "reason": message}, level="warning")
        return {"status": "error", "message": message}

    text = _result_text(result)
    if getattr(result, "is_error", False):
        # HA a refusé : son texte est plus utile au modèle qu'un message générique.
        return {
            "status": "error",
            "message": _without_sdk_wrapper(text, name) or _REFUSED_MESSAGE,
        }
    if not text:
        return {"status": "ok", "message": ""}
    return {"status": "ok", "message": text}


@contextlib.asynccontextmanager
async def _mcp_session(
    endpoint: _Endpoint, *, timeout: float
) -> AsyncIterator[ClientSession]:
    """Une session MCP complète, avec le porteur d'authentification."""
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {endpoint.token}"},
        timeout=httpx2.Timeout(timeout),
    )
    async with http:
        async with streamable_http_client(
            endpoint.url, http_client=http
        ) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timeout
            ) as session:
                await session.initialize()
                yield session


def _result_text(result: Any) -> str:
    """Concatène les blocs texte du résultat (HA ne rend que du texte)."""
    content = getattr(result, "content", None) or ()
    return "\n".join(
        block.text
        for block in content
        if getattr(block, "type", None) == "text" and block.text
    )


def _without_sdk_wrapper(text: str, name: str) -> str:
    """Retire le préfixe ajouté par le SDK : le modèle lit le texte de HA."""
    return text.removeprefix(f"Error executing tool {name}: ")


def _failure_message(exc: BaseException) -> str:
    """Classe un échec d'appel : délai dépassé ou maison injoignable."""
    leaves = _leaves(exc)
    if isinstance(exc, TimeoutError):
        return _TIMEOUT_MESSAGE
    for leaf in leaves:
        if isinstance(leaf, MCPError) and leaf.code == -32001:
            return _TIMEOUT_MESSAGE
        if isinstance(leaf, httpx2.TimeoutException):
            return _TIMEOUT_MESSAGE
    return _UNREACHABLE_MESSAGE


def _leaves(exc: BaseException) -> list[BaseException]:
    """Aplatit les groupes d'exceptions du transport (anyio)."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def _describe(exc: BaseException) -> str:
    """Résumé traçable d'une panne, sans en-têtes donc sans token."""
    return "; ".join(
        f"{type(leaf).__name__}: {leaf}" if str(leaf) else type(leaf).__name__
        for leaf in _leaves(exc)
    )
