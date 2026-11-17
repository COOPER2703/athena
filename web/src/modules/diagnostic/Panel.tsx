import { useId, type ReactNode } from "react";

import { cn } from "../../shared/cn";

export interface PanelProps {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * Section du Diagnostic : un titre de section, un filet, pas de carte ronde.
 *
 * La coquille ne fournit pas de primitive de panneau : chaque zone garde la
 * sienne pour ne pas imposer son esthétique aux autres.
 */
export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
}: PanelProps) {
  const titleId = useId();
  return (
    <section
      aria-labelledby={titleId}
      className={cn("border border-border bg-surface", className)}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h2
            id={titleId}
            className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
          >
            {title}
          </h2>
          {subtitle ? (
            <p className="mt-1 text-xs leading-relaxed text-subtle">{subtitle}</p>
          ) : null}
        </div>
        {actions ? (
          <div className="flex flex-wrap items-center gap-2">{actions}</div>
        ) : null}
      </header>
      <div className="px-4 py-4">{children}</div>
    </section>
  );
}

export interface Fact {
  label: string;
  value: ReactNode;
}

/** Liste de faits clé/valeur : la forme commune des panneaux d'observation. */
export function Facts({
  items,
  className,
}: {
  items: readonly Fact[];
  className?: string;
}) {
  return (
    <dl
      className={cn(
        "grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] items-baseline gap-x-4 gap-y-2",
        className,
      )}
    >
      {items.map((item) => (
        <div key={item.label} className="contents">
          <dt className="font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
            {item.label}
          </dt>
          <dd className="min-w-0 break-words font-mono text-xs text-text">
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
