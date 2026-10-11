from __future__ import annotations

from typing import Mapping

from server.core.ports import MemoryStore, ToolSpec
from server.core.tools import ToolRegistry

SAVE_MEMORY = "save_memory"
FORGET_MEMORY = "forget_memory"

_PROFILE = "profile"
_FACT = "fact"

_SAVE_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Le contenu à mémoriser, formulé à la troisième personne.",
        },
        "kind": {
            "type": "string",
            "enum": [_PROFILE, _FACT],
            "description": (
                "profile: préférence ou information stable sur l'utilisateur "
                "(toujours injectée). fact: fait durable rappelable par recherche."
            ),
        },
    },
    "required": ["content", "kind"],
}

_FORGET_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "Le contenu du souvenir à oublier (correspondance sémantique).",
        }
    },
    "required": ["content"],
}

_SAVE_SPEC = ToolSpec(
    name=SAVE_MEMORY,
    description=(
        "Mémorise durablement une information. kind='profile' pour une "
        "préférence ou un trait stable de l'utilisateur (fait épinglé, toujours "
        "présent à chaque session) ; kind='fact' pour un fait durable rappelé "
        "par recherche quand la conversation s'y rapporte."
    ),
    parameters=_SAVE_PARAMETERS,
)

_FORGET_SPEC = ToolSpec(
    name=FORGET_MEMORY,
    description=(
        "Oublie le souvenir le plus proche du contenu fourni (correspondance "
        "sémantique) et les tours de Journal qui l'ont produit."
    ),
    parameters=_FORGET_PARAMETERS,
)


def _as_text(value: object) -> str:
    """Coerce a loosely-typed Tool argument to trimmed text (never raises)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def register_memory_tools(
    registry: ToolRegistry,
    memory: MemoryStore,
    user_id: str,
    *,
    forget_max_distance: float = 0.6,
) -> ToolRegistry:
    """Register the two kernel memory Tools (ADR-0008) on ``registry``.

    ``save_memory(content, kind)`` writes a durable fact: ``profile`` pins it,
    ``fact`` does not. ``forget_memory(content)`` deletes the semantically
    nearest fact of this user (cosine distance below ``forget_max_distance``)
    and its source Journal turns. Both never raise: a Tool call always yields a
    status mapping instead.
    """

    async def save_memory(arguments: Mapping[str, object]) -> Mapping[str, object]:
        content = _as_text(arguments.get("content"))
        if not content:
            return {"status": "error", "message": "content is required"}
        kind = _as_text(arguments.get("kind")).lower()
        if kind not in (_PROFILE, _FACT):
            return {
                "status": "error",
                "message": "kind must be 'profile' or 'fact'",
            }
        pinned = kind == _PROFILE
        try:
            await memory.save(user_id, content, pinned=pinned)
        except Exception:
            return {"status": "error", "message": "save_memory failed"}
        return {
            "status": "ok",
            "kind": _PROFILE if pinned else _FACT,
            "pinned": pinned,
        }

    async def forget_memory(arguments: Mapping[str, object]) -> Mapping[str, object]:
        content = _as_text(arguments.get("content"))
        if not content:
            return {"status": "error", "message": "content is required"}
        try:
            removed = await memory.forget(user_id, content, forget_max_distance)
        except Exception:
            return {"status": "error", "message": "forget_memory failed"}
        return {"status": "removed" if removed else "not_found"}

    registry.register(_SAVE_SPEC, save_memory)
    registry.register(_FORGET_SPEC, forget_memory)
    return registry
