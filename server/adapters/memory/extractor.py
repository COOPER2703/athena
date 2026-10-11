from __future__ import annotations

import asyncio
import json
from typing import Callable

from server.adapters.trace import NullTraceSink, emit
from server.core.ports import TraceSink

_TRACE_SOURCE = "consolidation"

# Prompt repris de legacy/server/memory/consolidator.py (EXTRACTION_PROMPT).
EXTRACTION_PROMPT = """Tu es un extracteur de faits durables. À partir de fragments de conversation, extrais les faits individuels, durables et pertinents sur l'utilisateur.

Chaque fait doit :
- être une phrase courte, autonome et compréhensible hors contexte
- contenir une information durable (préférence, habitude, fait personnel, événement important)
- ne pas contenir d'opinions de l'assistant ou de métadonnées de conversation

Retourne un tableau JSON de chaînes de caractères. Si rien ne mérite d'être retenu, retourne un tableau vide [].

Exemples de faits à extraire :
- "L'utilisateur travaille comme développeur Python"
- "L'utilisateur habite à Lyon"
- "L'utilisateur préfère le café au thé"

Exemples de faits à ne PAS extraire :
- Salutations ou politesses
- Questions sans information personnelle
- Réponses de l'assistant"""

# ``generate`` reçoit (modèle, prompt) et renvoie le texte brut du modèle.
Generate = Callable[[str, str], str]


def _default_generate(api_key: str) -> Generate:
    def generate(model: str, prompt: str) -> str:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        return response.text or ""

    return generate


class GeminiFactExtractor:
    """``FactExtractor`` non-live adossé à ``generate_content`` (issue #30).

    Le modèle est distinct du modèle Live (``consolidation_model``). L'import de
    ``google.genai`` et l'appel bloquant sont faits hors de la boucle via
    ``asyncio.to_thread``. ``extract`` ne lève jamais : toute erreur (réseau,
    réponse non-JSON) renvoie ``[]``.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str,
        generate: Generate | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._model = model
        self._generate: Generate = generate or _default_generate(api_key)
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()

    async def extract(self, transcript: str) -> list[str]:
        if not transcript.strip():
            return []
        prompt = f"{EXTRACTION_PROMPT}\n\n---\n\n{transcript}"
        try:
            text = await asyncio.to_thread(self._generate, self._model, prompt)
        except Exception as exc:
            self._emit("extraction_failed", {"error": str(exc)}, level="error")
            return []
        facts = self._parse(text)
        self._emit(
            "extraction_done",
            {"facts": len(facts), "chars": len(transcript)},
            level="debug",
        )
        return facts

    @staticmethod
    def _parse(text: str | None) -> list[str]:
        if not text:
            return []
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            stripped = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            )
        try:
            facts = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        if not isinstance(facts, list):
            return []
        return [fact for fact in facts if isinstance(fact, str) and fact.strip()]

    def _emit(self, kind: str, payload: dict[str, object], *, level: str) -> None:
        emit(self._trace_sink, _TRACE_SOURCE, kind, payload=payload, level=level)
