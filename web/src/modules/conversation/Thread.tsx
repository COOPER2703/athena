import { useCallback, useEffect, useRef, useState } from "react";

import { describeError } from "../../api/errors";
import { zoneById } from "../../app/routing";
import { ConversationIcon, RefreshIcon } from "../../shared/icons";
import { pluralize } from "../../shared/format";
import { Button } from "../../shared/ui/Button";
import { EmptyState } from "../../shared/ui/EmptyState";
import { Notice } from "../../shared/ui/Notice";
import type { LiveTranscriptLine } from "./liveTranscript";
import { Message } from "./Message";

export interface ThreadProps {
  lines: readonly LiveTranscriptLine[];
  loading: boolean;
  error: unknown;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRetry: () => void;
  retentionDays: number;
}

/**
 * Fil unifié : texte, Tours vocaux, paroles vivantes et Annonces, du plus
 * ancien au plus récent.
 *
 * La pagination remonte vers le passé avec le curseur du serveur ; la fenêtre
 * de rétention est rappelée en permanence pour ne pas faire croire à un
 * historique complet. Les lignes encore vivantes (transcriptions reçues avant
 * leur entrée au Journal) sont annoncées poliment, une fois, sans déplacer le
 * focus ; le fil lui-même n'est pas une zone live, sinon la relecture
 * répéterait chaque tour.
 */
export function Thread({
  lines,
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

  const liveLines = lines.filter((line) => line.entry === null);
  const lastLive = liveLines[liveLines.length - 1];
  const [announcement, setAnnouncement] = useState("");
  const announcedRef = useRef("");
  const endRef = useRef<HTMLDivElement>(null);
  const anchoredRef = useRef(false);

  /** Amène la fin du fil dans la vue : ouverture, puis paroles vivantes. */
  const scrollToEnd = useCallback((behavior: ScrollBehavior) => {
    endRef.current?.scrollIntoView({ block: "end", behavior });
  }, []);

  // À la première lecture, on ouvre le fil sur son message le plus récent :
  // la page ne doit pas s'arrêter en haut, sur le plus ancien de la fenêtre.
  // Une seule fois par visite, pour ne jamais arracher quelqu'un qui remonte
  // l'historique.
  useEffect(() => {
    if (anchoredRef.current || loading || lines.length === 0) {
      return;
    }
    anchoredRef.current = true;
    requestAnimationFrame(() => {
      scrollToEnd("auto");
    });
  }, [loading, lines.length, scrollToEnd]);

  // Une nouvelle parole vivante est annoncée une seule fois, et le fil suit :
  // la personne voit ses mots arriver sans quitter la barre vocale. Quand une
  // ligne rejoint le Journal, la dernière ligne vivante peut redevenir une
  // parole déjà annoncée : on ne la répète pas.
  useEffect(() => {
    if (lastLive === undefined) {
      return;
    }
    const marker = `${lastLive.key}\u0000${lastLive.text}`;
    if (announcedRef.current === marker) {
      return;
    }
    announcedRef.current = marker;
    setAnnouncement(
      `${lastLive.role === "user" ? "Vous" : "Athena"} : ${lastLive.text}`,
    );
    const reduced =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    scrollToEnd(reduced ? "auto" : "smooth");
  }, [lastLive?.key, lastLive?.text, scrollToEnd]);

  return (
    <section
      aria-label="Fil de la conversation"
      className="flex flex-col gap-4"
    >
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="sr-only"
      >
        {announcement}
      </p>

      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
        <p className="font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
          {retention}
        </p>
        {hasMore ? (
          <Button
            variant="ghost"
            size="square"
            onClick={onLoadMore}
            disabled={loadingMore}
            aria-busy={loadingMore}
            aria-label={
              loadingMore
                ? "Chargement des messages plus anciens"
                : "Charger les messages plus anciens"
            }
            title={
              loadingMore
                ? "Chargement des messages plus anciens"
                : "Charger les messages plus anciens"
            }
            icon={<RefreshIcon className={loadingMore ? "animate-spin" : undefined} />}
          />
        ) : null}
      </div>

      {error !== null && lines.length > 0 ? (
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

      {loading && lines.length === 0 ? (
        <p role="status" className="text-sm text-muted">
          Lecture du fil…
        </p>
      ) : null}

      {error !== null && lines.length === 0 && !loading ? (
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

      {!loading && error === null && lines.length === 0 && zone !== null ? (
        <EmptyState
          icon={<ConversationIcon className="h-5 w-5" />}
          title={zone.emptyTitle}
          body={zone.emptyBody}
          hint={zone.emptyHint}
        />
      ) : null}

      {lines.length > 0 ? (
        <ol className="flex flex-col gap-5">
          {lines.map((line) => (
            <li key={line.key}>
              <Message line={line} />
            </li>
          ))}
        </ol>
      ) : null}

      {/* `scroll-mb-20` dégage la barre de saisie collante en mobile. */}
      <div ref={endRef} className="scroll-mb-20" />
    </section>
  );
}
