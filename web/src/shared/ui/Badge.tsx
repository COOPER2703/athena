import type { ReactNode } from "react";

import { cn } from "../cn";

export type BadgeTone = "neutral" | "accent" | "danger" | "warning";

const TONES: Readonly<Record<BadgeTone, string>> = {
  neutral: "border-border text-muted",
  accent: "border-accent-line bg-accent-soft text-accent",
  danger: "border-danger-line bg-danger-soft text-danger",
  warning: "border-warning-line bg-warning-soft text-warning",
};

/** Étiquette compacte : état, source, nature d'un Jeton. */
export function Badge({
  tone = "neutral",
  children,
  className,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 font-mono text-[0.6875rem] uppercase tracking-[0.1em]",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
