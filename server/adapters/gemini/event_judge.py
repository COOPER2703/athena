from __future__ import annotations

import asyncio
import json
from typing import Callable, Sequence

from server.adapters.trace import NullTraceSink, emit
from server.core.ports import Judgment, TraceSink

_TRACE_SOURCE = "judge"

# Le fournisseur renvoie parfois 503/429 sur un pic de charge. Le SDK retente
# déjà, mais un échec persistant rend `announce=false` (faute fermée) : sans
# retry applicatif, une Session automatique est silencieusement perdue. Trois
# tentatives avec un backoff court suffisent, sans allonger sensiblement le
# jugement.
JUDGE_ATTEMPTS = 3
JUDGE_RETRY_DELAY = 0.5

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
    via ``asyncio.to_thread``. Un pic de charge du fournisseur (503/429) est
    retenté quelques fois avec un backoff court avant la faute fermée. ``judge``
    ne lève jamais : toute erreur persistante (réseau, réponse non-JSON) renvoie
    ``Judgment(announce=False, failed=True)`` — faute fermée, on ne dérange pas
    l'utilisateur dans le doute. ``failed`` rend la panne observable : elle n'est
    jamais confondue avec un fait jugé banal.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str,
        prompt: str,
        generate: Generate | None = None,
        trace_sink: TraceSink | None = None,
        attempts: int = JUDGE_ATTEMPTS,
        retry_delay: float = JUDGE_RETRY_DELAY,
    ) -> None:
        self._model = model
        self._prompt = prompt
        self._generate: Generate = generate or _default_generate(api_key)
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()
        self._attempts = max(1, attempts)
        self._retry_delay = max(0.0, retry_delay)

    async def judge(self, facts: Sequence[str]) -> Judgment:
        if not facts:
            return Judgment(announce=False)
        prompt = f"{self._prompt}\n\n---\n\n" + "\n".join(
            f"- {fact}" for fact in facts
        )
        text = ""
        for attempt in range(1, self._attempts + 1):
            try:
                text = await asyncio.to_thread(self._generate, self._model, prompt)
                break
            except Exception as exc:
                if attempt < self._attempts:
                    self._emit(
                        "judgment_retry",
                        {"attempt": attempt, "error": str(exc)},
                        level="warning",
                    )
                    await asyncio.sleep(self._retry_delay * attempt)
                    continue
                self._emit(
                    "judgment_failed",
                    {"reason": "generator_error", "error": str(exc)},
                    level="error",
                )
                return Judgment(announce=False, failed=True)
        verdict = self._parse(text)
        if verdict.failed:
            # Verdict manquant (réponse non-JSON, malformée, vide) : tracé comme
            # un échec, jamais comme un fait jugé banal.
            self._emit(
                "judgment_failed",
                {"facts": len(facts), "reason": "unparseable"},
                level="error",
            )
            return verdict
        self._emit(
            "judgment_done",
            {"facts": len(facts), "announce": verdict.announce},
            level="debug",
        )
        return verdict

    @staticmethod
    def _parse(text: str | None) -> Judgment:
        if not text:
            return Judgment(announce=False, failed=True)
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            stripped = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            )
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return Judgment(announce=False, failed=True)
        if not isinstance(data, dict):
            return Judgment(announce=False, failed=True)
        announce = data.get("announce")
        if not isinstance(announce, bool):
            return Judgment(announce=False, failed=True)
        if not announce:
            return Judgment(announce=False)
        message = data.get("message")
        if not isinstance(message, str):
            message = ""
        return Judgment(announce=True, message=message)

    def _emit(self, kind: str, payload: dict[str, object], *, level: str) -> None:
        emit(self._trace_sink, _TRACE_SOURCE, kind, payload=payload, level=level)
