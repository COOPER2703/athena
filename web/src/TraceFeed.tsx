import { useEffect, useRef, useState } from "react";

import {
  EMPTY_FILTERS,
  LIVE_VIEW,
  availableLevels,
  availableSources,
  displayedEntries,
  pauseView,
  resumeView,
  viewAfterHello,
  type FeedView,
  type TraceFilters,
} from "./filters";
import {
  formatPayload,
  formatTimestamp,
  connectionLabel,
  traceLevelClass,
  type ConnectionStatus,
  type FeedState,
  type TraceEntry,
} from "./trace";

/**
 * Panneau de traces alimenté par le flux de la page.
 *
 * Le flux (connexion, reconnexion, backlog) vit plus haut, dans
 * `useConsoleStream` : ce panneau ne fait que filtrer, geler et faire défiler
 * ce qu'il reçoit. Une pause fige l'affichage mais pas les données.
 */
export function TraceFeed({
  feed,
  connection,
}: {
  feed: FeedState;
  connection: ConnectionStatus;
}) {
  const [filters, setFilters] = useState<TraceFilters>(EMPTY_FILTERS);
  const [view, setView] = useState<FeedView>(LIVE_VIEW);
  const [autoScroll, setAutoScroll] = useState(true);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const levels = availableLevels(feed.entries);
  const sources = availableSources(feed.entries);
  const entries = displayedEntries(feed.entries, filters, view);
  const pending = view.paused
    ? Math.max(0, feed.entries.length - view.snapshot.length)
    : 0;
  const filtersActive =
    filters.level !== "" || filters.source !== "" || filters.text.trim() !== "";

  useEffect(() => {
    if (!autoScroll || view.paused) {
      return;
    }
    const list = scrollRef.current;
    if (list !== null) {
      list.scrollTop = list.scrollHeight;
    }
  }, [feed.entries, autoScroll, view.paused]);

  // Une reconnexion remplace le flux : la vue gelée repart du nouveau backlog
  // tout en conservant la pause demandée par l'utilisateur. Sans cela, elle
  // garderait les traces du processus précédent après un redémarrage serveur.
  useEffect(() => {
    setView((current) => viewAfterHello(current, feed.entries));
  }, [feed.generation]);

  return (
    <section className="panel trace-panel" aria-labelledby="traces-title">
      <div className="panel__header">
        <h2 className="panel__title" id="traces-title">
          Traces
        </h2>
        <div className="trace-panel__controls">
          <span className={`status status--${connection}`}>
            {connectionLabel(connection)}
          </span>
        </div>
      </div>

      <div className="trace-filters">
        <label className="field field--filter" htmlFor="trace-level">
          <span className="field__label">Niveau</span>
          <select
            className="field__input mono"
            id="trace-level"
            value={filters.level}
            onChange={(event) =>
              setFilters((current) => ({
                ...current,
                level: event.target.value,
              }))
            }
          >
            <option value="">Tous</option>
            {levels.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
        </label>

        <label className="field field--filter" htmlFor="trace-source">
          <span className="field__label">Source</span>
          <select
            className="field__input mono"
            id="trace-source"
            value={filters.source}
            onChange={(event) =>
              setFilters((current) => ({
                ...current,
                source: event.target.value,
              }))
            }
          >
            <option value="">Toutes</option>
            {sources.map((source) => (
              <option key={source} value={source}>
                {source}
              </option>
            ))}
          </select>
        </label>

        <label className="field field--search" htmlFor="trace-text">
          <span className="field__label">Texte</span>
          <input
            className="field__input"
            id="trace-text"
            type="search"
            value={filters.text}
            placeholder="source, kind ou payload…"
            onChange={(event) =>
              setFilters((current) => ({
                ...current,
                text: event.target.value,
              }))
            }
            spellCheck={false}
          />
        </label>

        <div className="trace-filters__actions">
          <button
            className="button"
            type="button"
            aria-pressed={view.paused}
            onClick={() =>
              setView((current) =>
                current.paused ? resumeView() : pauseView(feed.entries),
              )
            }
          >
            {view.paused ? "Reprendre" : "Pause"}
          </button>
          <button
            className="button"
            type="button"
            aria-pressed={autoScroll}
            onClick={() => setAutoScroll((value) => !value)}
          >
            {autoScroll ? "Défilement auto" : "Défilement suspendu"}
          </button>
          <button
            className="button"
            type="button"
            disabled={!filtersActive}
            onClick={() => setFilters(EMPTY_FILTERS)}
          >
            Réinitialiser
          </button>
        </div>
      </div>

      {feed.truncated > 0 ? (
        <p className="notice notice--warning" role="status">
          Flux tronqué : {feed.truncated} entrée(s) perdue(s).
        </p>
      ) : null}

      {feed.browserDropped > 0 ? (
        <p className="notice notice--warning" role="status">
          Flux tronqué côté navigateur : {feed.browserDropped} entrée(s) plus
          ancienne(s) non conservée(s).
        </p>
      ) : null}

      {view.paused ? (
        <p className="notice notice--warning" role="status">
          Affichage gelé{pending > 0 ? ` · ${pending} nouvelle(s) trace(s)` : ""}.
        </p>
      ) : null}

      <div className="trace-list" ref={scrollRef} aria-live="polite">
        {entries.length === 0 ? (
          <p className="notice">
            {feed.entries.length === 0
              ? "En attente de traces…"
              : "Aucune trace ne correspond aux filtres."}
          </p>
        ) : (
          entries.map((entry, index) => (
            <TraceRow
              key={`${entry.timestamp}-${entry.source}-${entry.kind}-${index}`}
              entry={entry}
            />
          ))
        )}
      </div>
    </section>
  );
}

function TraceRow({ entry }: { entry: TraceEntry }) {
  const payload = formatPayload(entry.payload);
  return (
    <article className={`trace trace--${traceLevelClass(entry.level)}`}>
      <span className="trace__time">{formatTimestamp(entry.timestamp)}</span>
      <span className="trace__level">{entry.level.toUpperCase()}</span>
      <span className="trace__source">[{entry.source}]</span>
      <span className="trace__kind">{entry.kind}</span>
      {payload ? (
        <details className="trace__payload">
          <summary>payload</summary>
          <pre>{payload}</pre>
        </details>
      ) : null}
    </article>
  );
}
