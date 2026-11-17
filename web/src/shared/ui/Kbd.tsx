import type { ReactNode } from "react";

import { cn } from "../cn";

/** Touche de clavier affichée dans l'interface. */
export function Kbd({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <kbd
      className={cn(
        "inline-flex min-w-6 items-center justify-center rounded-sm border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[0.6875rem] leading-none text-muted",
        className,
      )}
    >
      {children}
    </kbd>
  );
}
