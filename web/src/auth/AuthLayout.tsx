import type { ReactNode } from "react";

import { ThemeToggle } from "../design/ThemeToggle";
import { BrandMark } from "../shared/icons";

export interface AuthLayoutProps {
  eyebrow: string;
  title: string;
  summary: string;
  children: ReactNode;
  footer?: ReactNode;
}

/**
 * Cadre commun de la connexion et de l'installation : une seule carte sur une
 * trame discrète, le thème toujours accessible, aucun verre dépoli.
 */
export function AuthLayout({
  eyebrow,
  title,
  summary,
  children,
  footer,
}: AuthLayoutProps) {
  return (
    <div className="relative flex min-h-dvh flex-col bg-canvas">
      <div
        aria-hidden="true"
        className="hud-grid pointer-events-none absolute inset-0"
      />
      <header className="relative flex items-center justify-between gap-4 px-5 py-5 sm:px-8">
        <div className="flex items-center gap-3 text-accent">
          <BrandMark className="h-6 w-6" />
          <span className="font-mono text-xs uppercase tracking-[0.28em] text-text">
            Athena
          </span>
          <span className="font-mono text-[0.6875rem] uppercase tracking-[0.18em] text-subtle">
            Console
          </span>
        </div>
        <ThemeToggle />
      </header>

      <main
        id="main"
        className="relative flex flex-1 items-center justify-center px-5 pb-16 pt-6 sm:px-8"
      >
        <div className="w-full max-w-md rounded-sm border border-border bg-surface p-6 sm:p-8">
          <p className="font-mono text-[0.6875rem] uppercase tracking-[0.2em] text-accent">
            {eyebrow}
          </p>
          <h1 className="mt-3 text-2xl font-medium tracking-tight text-text">
            {title}
          </h1>
          <p className="mt-2 text-sm leading-relaxed text-muted">{summary}</p>
          <div className="mt-7">{children}</div>
          {footer ? (
            <div className="mt-7 border-t border-border pt-4 font-mono text-xs leading-relaxed text-subtle">
              {footer}
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}
