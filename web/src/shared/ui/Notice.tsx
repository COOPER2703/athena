import type { ReactNode } from "react";

import { cn } from "../cn";

export type NoticeTone = "info" | "warning" | "danger" | "success";

export interface NoticeProps {
  tone?: NoticeTone;
  title?: string;
  children: ReactNode;
  className?: string;
}

const TONES: Readonly<Record<NoticeTone, string>> = {
  info: "border-accent-line bg-accent-soft text-text",
  warning: "border-warning-line bg-warning-soft text-warning",
  danger: "border-danger-line bg-danger-soft text-danger",
  success: "border-accent-line bg-accent-soft text-accent",
};

/** Bandeau d'information : jamais de couleur seule, un titre porte le sens. */
export function Notice({
  tone = "info",
  title,
  children,
  className,
}: NoticeProps) {
  return (
    <div
      role={tone === "danger" || tone === "warning" ? "alert" : "status"}
      className={cn(
        "rounded-sm border px-4 py-3 text-sm leading-relaxed",
        TONES[tone],
        className,
      )}
    >
      {title ? <p className="mb-1 font-medium">{title}</p> : null}
      <div className={title ? "text-muted" : undefined}>{children}</div>
    </div>
  );
}
