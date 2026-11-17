import { useId, type ReactNode } from "react";

import { cn } from "../cn";

export interface EmptyStateProps {
  icon: ReactNode;
  title: string;
  body: string;
  hint?: string;
  children?: ReactNode;
  className?: string;
}

/**
 * État vide d'une zone.
 *
 * Il occupe la place du module à venir : le module remplace l'état vide sans
 * toucher à la coquille, au titre ni à la navigation.
 */
export function EmptyState({
  icon,
  title,
  body,
  hint,
  children,
  className,
}: EmptyStateProps) {
  const titleId = useId();

  return (
    <section
      aria-labelledby={titleId}
      className={cn(
        "relative mx-auto flex w-full max-w-2xl flex-col gap-5 rounded-sm border border-border bg-surface px-6 py-10 sm:px-10 sm:py-12",
        className,
      )}
    >
      {/* Repères de coin : un détail HUD, sans verre ni ombre portée. */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute left-0 top-0 h-3 w-3 border-l border-t border-accent-line"
      />
      <span
        aria-hidden="true"
        className="pointer-events-none absolute bottom-0 right-0 h-3 w-3 border-b border-r border-accent-line"
      />
      <span className="flex h-10 w-10 items-center justify-center rounded-sm border border-accent-line bg-accent-soft text-accent">
        {icon}
      </span>
      <div className="flex flex-col gap-2">
        <h2
          id={titleId}
          className="text-xl font-medium tracking-tight text-text"
        >
          {title}
        </h2>
        <p className="max-w-prose text-sm leading-relaxed text-muted">{body}</p>
      </div>
      {hint ? (
        <p className="flex items-start gap-2 border-t border-border pt-4 font-mono text-xs leading-relaxed text-subtle">
          <span aria-hidden="true" className="text-accent">
            →
          </span>
          {hint}
        </p>
      ) : null}
      {children ? <div className="flex flex-wrap gap-2">{children}</div> : null}
    </section>
  );
}
