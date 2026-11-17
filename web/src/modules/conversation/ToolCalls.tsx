import type { ToolCallView } from "./turn";

/**
 * Tools appelés pendant le Tour : nom, statut et arguments repliables.
 *
 * Le panneau ne reçoit que ce que le flux a publié ; aucune configuration ni
 * aucun Secret n'y transite.
 */
export function ToolCalls({ tools }: { tools: readonly ToolCallView[] }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="font-mono text-[0.625rem] uppercase tracking-[0.14em] text-subtle">
        Tools appelés
      </p>
      <ul className="flex flex-col gap-1.5">
        {tools.map((tool, index) => (
          <li
            key={`${tool.name}-${index}`}
            className="rounded-sm border border-border bg-surface px-3 py-2"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs text-text">{tool.name}</span>
              <span
                className={
                  tool.status === "ok"
                    ? "font-mono text-[0.625rem] uppercase tracking-[0.1em] text-accent"
                    : "font-mono text-[0.625rem] uppercase tracking-[0.1em] text-danger"
                }
              >
                {tool.status === "ok" ? "OK" : "Échec"}
              </span>
            </div>
            {tool.arguments !== "" ? (
              <details className="mt-1.5">
                <summary className="cursor-pointer font-mono text-[0.6875rem] text-muted hover:text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
                  Arguments
                </summary>
                <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words font-mono text-[0.6875rem] leading-relaxed text-muted">
                  {tool.arguments}
                </pre>
              </details>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
