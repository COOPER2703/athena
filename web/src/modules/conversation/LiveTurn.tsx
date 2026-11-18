import { Badge, type BadgeTone } from "../../shared/ui/Badge";
import { Notice } from "../../shared/ui/Notice";
import { Activity } from "./Activity";
import { OriginBadge } from "./OriginBadge";
import { ToolCalls } from "./ToolCalls";
import { doneReasonLabel, type TurnState } from "./turn";

function statusOf(turn: TurnState): { label: string; tone: BadgeTone } {
  switch (turn.phase) {
    case "streaming":
      return { label: "En cours", tone: "accent" };
    case "canceled":
      return { label: "Interrompu", tone: "warning" };
    case "busy":
      return { label: "Occupée", tone: "warning" };
    case "error":
      return { label: "Échec", tone: "danger" };
    case "done":
      return {
        label: doneReasonLabel(turn.reason ?? "completed"),
        tone: "neutral",
      };
  }
}

/**
 * Tour en cours de la Console : le message envoyé, la réponse qui s'écrit, les
 * Tools appelés et l'état du flux. Le Tour disparaît dès que le Journal le
 * reprend, pour ne jamais doubler le fil.
 */
export function LiveTurn({ turn }: { turn: TurnState }) {
  const status = statusOf(turn);
  const streaming = turn.phase === "streaming";

  return (
    <section aria-label="Tour en cours" className="flex flex-col gap-4">
      <article className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center justify-end gap-2 font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
          <OriginBadge origin="text" />
          <span>Vous</span>
        </div>
        <p className="ml-auto max-w-[min(42rem,90%)] whitespace-pre-wrap break-words rounded-sm border border-border bg-surface px-4 py-3 text-sm leading-relaxed text-text">
          {turn.message}
        </p>
      </article>

      <article className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2 font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
          <OriginBadge origin="text" />
          <span>Athena</span>
          <Badge tone={status.tone}>{status.label}</Badge>
        </div>

        <div className="flex max-w-[52rem] flex-col gap-3 border-l-2 border-accent-line py-1 pl-4">
          {turn.tools.length > 0 ? <ToolCalls tools={turn.tools} /> : null}

          {turn.reply !== "" ? (
            <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-text">
              {turn.reply}
            </p>
          ) : streaming ? (
            <p className="text-sm leading-relaxed text-subtle">
              Athena prépare sa réponse…
            </p>
          ) : null}

          {streaming ? <Activity /> : null}

          {turn.phase === "canceled" ? (
            <Notice tone="info" title="Tour interrompu">
              La réponse s'arrête là. Le message envoyé reste dans le fil.
            </Notice>
          ) : null}
        </div>
      </article>
    </section>
  );
}
