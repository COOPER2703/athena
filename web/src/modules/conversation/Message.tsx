import type { ThreadEntry } from "../../api/thread";
import { cn } from "../../shared/cn";
import { formatIsoDate } from "../../shared/format";
import { OriginBadge } from "./OriginBadge";

/**
 * Une entrée du fil : message de l'utilisateur à droite, réponse ou Annonce
 * d'Athena en pleine largeur avec un filet d'accent. L'origine est toujours
 * écrite, jamais seulement colorée.
 */
export function Message({ entry }: { entry: ThreadEntry }) {
  const user = entry.role === "user";
  const announcement = entry.origin === "event";
  const author = user ? "Vous" : announcement ? "Athena · Annonce" : "Athena";

  return (
    <article className="flex flex-col gap-1.5">
      <div
        className={cn(
          "flex flex-wrap items-center gap-2 font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle",
          user && "justify-end",
        )}
      >
        <OriginBadge origin={entry.origin} />
        <span>{author}</span>
        <time dateTime={entry.createdAt}>
          {formatIsoDate(entry.createdAt)}
        </time>
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
        {entry.text}
      </div>
    </article>
  );
}
