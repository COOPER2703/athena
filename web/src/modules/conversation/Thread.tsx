import type { ThreadEntry } from "../../api/thread";
import { describeError } from "../../api/errors";
import { zoneById } from "../../app/routing";
import { ConversationIcon, RefreshIcon } from "../../shared/icons";
import { pluralize } from "../../shared/format";
import { Button } from "../../shared/ui/Button";
import { EmptyState } from "../../shared/ui/EmptyState";
import { Notice } from "../../shared/ui/Notice";
import { Message } from "./Message";

export interface ThreadProps {
  entries: readonly ThreadEntry[];
  loading: boolean;
  error: unknown;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
  retentionDays: number;
}

/**
 * Fil unifié : texte, tours vocaux et Annonces, du plus ancien au plus récent.
 *
 * La pagination remonte vers le passé avec le curseur du serveur ; la fenêtre
 * de rétention est rappelée en permanence pour ne pas faire croire à un
 * historique complet.
 */
export function Thread({
  entries,
  loading,
  error,
  hasMore,
  loadingMore,
  onLoadMore,
  onRetry,
  retentionDays,
}: ThreadProps) {
  const zone = zoneById("conversation");
  const retention =
    retentionDays > 0
      ? `Fenêtre de rétention : ${pluralize(retentionDays, "jour")}.`
      : "Fenêtre de rétention inconnue.";

  return (
    <section
      aria-label="Fil de la conversation"
      className="flex flex-col gap-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
        <p className="font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
          {retention}
        </p>
        {hasMore ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={onLoadMore}
            disabled={loadingMore}
            icon={<RefreshIcon />}
          >
            {loadingMore ? "Chargement…" : "Charger les messages plus anciens"}
          </Button>
        ) : null}
      </div>

      {error !== null && entries.length > 0 ? (
        <Notice tone="warning" title="Fil non actualisé">
          {describeError(error)}{" "}
          <button
            type="button"
            onClick={onRetry}
            className="font-mono text-xs underline decoration-dotted underline-offset-4 hover:text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            Réessayer
          </button>
        </Notice>
      ) : null}

      {loading && entries.length === 0 ? (
        <p role="status" className="text-sm text-muted">
          Lecture du fil…
        </p>
      ) : null}

      {error !== null && entries.length === 0 && !loading ? (
        <Notice tone="danger" title="Fil indisponible">
          <p>{describeError(error)}</p>
          <div className="mt-3">
            <Button
              variant="secondary"
              size="sm"
              onClick={onRetry}
              icon={<RefreshIcon />}
            >
              Réessayer
            </Button>
          </div>
        </Notice>
      ) : null}

      {!loading && error === null && entries.length === 0 && zone !== null ? (
        <EmptyState
          icon={<ConversationIcon className="h-5 w-5" />}
          title={zone.emptyTitle}
          body={zone.emptyBody}
          hint={zone.emptyHint}
        />
      ) : null}

      {entries.length > 0 ? (
        <ol className="flex flex-col gap-5">
          {entries.map((entry) => (
            <li key={entry.id}>
              <Message entry={entry} />
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
