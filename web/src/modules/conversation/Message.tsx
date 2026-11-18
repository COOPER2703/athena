import { cn } from "../../shared/cn";
import { formatIsoDate } from "../../shared/format";
import type { LiveTranscriptLine } from "./liveTranscript";
import { OriginBadge } from "./OriginBadge";

/**
 * Une ligne du fil : entrée du Journal ou parole encore vivante.
 *
 * Message de l'utilisateur à droite, réponse ou Annonce d'Athena en pleine
 * largeur avec un filet d'accent. L'origine est toujours écrite, jamais
 * seulement colorée. Une ligne vivante n'a pas encore d'horodatage : elle
 * porte « en direct » jusqu'à ce que le Journal la reprenne.
 */
export function Message({ line }: { line: LiveTranscriptLine }) {
  const user = line.role === "user";
  const announcement = line.origin === "event";
  const author = user ? "Vous" : announcement ? "Athena · Annonce" : "Athena";

  return (
    <article className="flex flex-col gap-1.5">
      <div
        className={cn(
          "flex flex-wrap items-center gap-2 font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle",
          user && "justify-end",
        )}
      >
        <OriginBadge origin={line.origin} />
        <span>{author}</span>
        {line.entry !== null ? (
          <time dateTime={line.entry.createdAt}>
            {formatIsoDate(line.entry.createdAt)}
          </time>
        ) : (
          <span className="text-accent">en direct</span>
        )}
      </div>
      <div
        className={cn(
          "whitespace-pre-wrap break-words text-sm leading-relaxed text-text",
          user
            ? "ml-auto max-w-[min(42rem,90%)] rounded-sm border border-border bg-surface px-4 py-3"
            : "max-w-[52rem] border-l-2 py-1 pl-4",
          !user && (announcement ? "border-warning-line" : "border-accent-line"),
        )}
      >
        {line.text}
      </div>
    </article>
  );
}
