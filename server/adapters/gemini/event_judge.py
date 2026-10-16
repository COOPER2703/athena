from __future__ import annotations

import asyncio
import json
from typing import Callable, Sequence

from server.adapters.trace import NullTraceSink, emit
from server.core.ports import Judgment, TraceSink

_TRACE_SOURCE = "judge"

# ``generate`` reçoit (modèle, prompt) et renvoie le texte brut du modèle.
Generate = Callable[[str, str], str]


def _default_generate(api_key: str) -> Generate:
    def generate(model: str, prompt: str) -> str:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        return response.text or ""

    return generate


class GeminiEventJudge:
    """``EventJudge`` non-live adossé à ``generate_content`` (ADR-0007, amendé).

    Le verdict (parler ou se taire) vient d'un modèle **texte**, jamais du modèle
    Live vocal : un modèle Live répond toujours à un tour utilisateur, il ne sait
    pas « se taire ». Le modèle est distinct du modèle Live (``judge_model``).
    L'import de ``google.genai`` et l'appel bloquant sont faits hors de la boucle
    via ``asyncio.to_thread``. ``judge`` ne lève jamais : toute erreur (réseau,
    réponse non-JSON) renvoie ``Judgment(announce=False)`` — faute fermée, on ne
    dérange pas l'utilisateur dans le doute.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str,
        prompt: str,
        generate: Generate | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._model = model
        self._prompt = prompt
        self._generate: Generate = generate or _default_generate(api_key)
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()

    async def judge(self, facts: Sequence[str]) -> Judgment:
        if not facts:
            return Judgment(announce=False)
        prompt = f"{self._prompt}\n\n---\n\n" + "\n".join(
            f"- {fact}" for fact in facts
        )
        try:
            text = await asyncio.to_thread(self._generate, self._model, prompt)
        except Exception as exc:
            self._emit("judgment_failed", {"error": str(exc)}, level="error")
            return Judgment(announce=False)
        verdict = self._parse(text)
        self._emit(
            "judgment_done",
            {"facts": len(facts), "announce": verdict.announce},
            level="debug",
        )
        return verdict

    @staticmethod
    def _parse(text: str | None) -> Judgment:
        if not text:
            return Judgment(announce=False)
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            stripped = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            )
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return Judgment(announce=False)
        if not isinstance(data, dict):
            return Judgment(announce=False)
        announce = data.get("announce")
        if not isinstance(announce, bool) or not announce:
            return Judgment(announce=False)
        message = data.get("message")
        if not isinstance(message, str):
            message = ""
        return Judgment(announce=True, message=message)

    def _emit(self, kind: str, payload: dict[str, object], *, level: str) -> None:
        emit(self._trace_sink, _TRACE_SOURCE, kind, payload=payload, level=level)
