import { useEffect, useRef } from "react";

import { cn } from "../../shared/cn";
import { OriginBadge } from "./OriginBadge";
import type { VoiceLine } from "./voice";

/**
 * Transcription vivante de la conversation vocale.
 *
 * Les mots de l'utilisateur arrivent au fil de l'eau, la réponse d'Athena en
 * fin de tour (comportement de Gemini Live). La liste est une zone
 * `aria-live` polie : une nouvelle ligne est annoncée sans déplacer le focus.
 */
export function VoiceTranscript({ lines }: { lines: readonly VoiceLine[] }) {
  const endRef = useRef<HTMLDivElement>(null);
  const count = lines.length;

  useEffect(() => {
    if (count === 0) {
      return;
    }
    const reduced =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    endRef.current?.scrollIntoView({
      block: "nearest",
      behavior: reduced ? "auto" : "smooth",
    });
  }, [count]);

  if (count === 0) {
    return null;
  }

  return (
    <section
      aria-label="Transcription de la conversation vocale"
      className="flex flex-col gap-3 rounded-sm border border-border bg-surface px-4 py-3"
    >
      <p className="font-mono text-[0.625rem] uppercase tracking-[0.14em] text-subtle">
        Transcription
      </p>
      <ol
        aria-live="polite"
        aria-relevant="additions"
        className="flex flex-col gap-2"
      >
        {lines.map((line, index) => (
          <li
            key={`${index}-${line.role}`}
            className={cn(
              "flex flex-col gap-1",
              line.role === "user" ? "items-end text-right" : "items-start",
            )}
          >
            <span className="flex items-center gap-2 font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
              <OriginBadge origin="voice" />
              <span>{line.role === "user" ? "Vous" : "Athena"}</span>
            </span>
            <p
              className={cn(
                "max-w-[min(42rem,90%)] whitespace-pre-wrap break-words rounded-sm border px-3 py-2 text-sm leading-relaxed",
                line.role === "user"
                  ? "border-border bg-surface-2 text-text"
                  : "border-accent-line bg-accent-soft text-text",
              )}
            >
              {line.text}
            </p>
          </li>
        ))}
      </ol>
      <div ref={endRef} />
    </section>
  );
}
