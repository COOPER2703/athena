import { useEffect, useMemo, useState } from "react";

import { Badge } from "../../shared/ui/Badge";
import { Button } from "../../shared/ui/Button";
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
import type { TraceBufferInfo } from "./model";
import { Panel } from "./Panel";
import {
  formatTraceLevel,
  formatTracePayload,
  formatTraceTime,
  traceLevelTone,
  type FeedState,
  type TraceEntry,
} from "./stream";

export interface TraceFeedProps {
  feed: FeedState;
  buffer: TraceBufferInfo | null;
}

/**
 * Traces du flux unique, filtrables par niveau et par source.
 *
 * Le flux continue d'arriver pendant une pause : seule la vue est gelée. La
 * borne du Tampon de traces est affichée en permanence, avec les pertes
 * éventuelles (serveur ou navigateur) : une omission n'est jamais silencieuse.
 */
export function TraceFeed({ feed, buffer }: TraceFeedProps) {
  const [filters, setFilters] = useState<TraceFilters>(EMPTY_FILTERS);
  const [view, setView] = useState<FeedView>(LIVE_VIEW);

  // Un nouveau `hello` remplace le flux : la vue gelée repart du nouveau
  // backlog, sinon une reconnexion pendant une pause afficherait l'ancien
  // processus.
  useEffect(() => {
    setView((current) => viewAfterHello(current, feed.entries));
  }, [feed.generation]);

  const entries = useMemo(
    () => displayedEntries(feed.entries, filters, view),
    [feed.entries, filters, view],
  );
  const levels = useMemo(() => availableLevels(feed.entries), [feed.entries]);
  const sources = useMemo(() => availableSources(feed.entries), [feed.entries]);

  const togglePause = () => {
    setView((current) =>
      current.paused ? resumeView() : pauseView(feed.entries),
    );
  };

  return (
    <Panel
      title="Traces"
      subtitle="Le flux d'observation du serveur, filtrable et borné."
      actions={
        <>
          {view.paused ? <Badge tone="warning">Vue gelée</Badge> : null}
          <Button
            size="sm"
            variant="secondary"
            aria-pressed={view.paused}
            onClick={togglePause}
          >
            {view.paused ? "Reprendre" : "Pause"}
          </Button>
        </>
      }
    >
      <div className="flex flex-wrap items-end gap-3">
        <FilterSelect
          label="Niveau"
          value={filters.level}
          onChange={(level) => setFilters((current) => ({ ...current, level }))}
          options={levels}
          render={(level) => formatTraceLevel(level)}
        />
        <FilterSelect
          label="Source"
          value={filters.source}
          onChange={(source) =>
            setFilters((current) => ({ ...current, source }))
          }
          options={sources}
          render={(source) => source}
        />
        <label className="flex min-w-[12rem] flex-1 flex-col gap-1">
          <span className="font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
            Recherche
          </span>
          <input
            type="search"
            value={filters.text}
            onChange={(event) =>
              setFilters((current) => ({ ...current, text: event.target.value }))
            }
            placeholder="kind, source, payload…"
            className="h-8 rounded-sm border border-border bg-canvas px-2 font-mono text-xs text-text placeholder:text-subtle transition-quiet hover:border-border-strong focus:border-accent"
          />
        </label>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
        <p className="font-mono text-[0.6875rem] text-subtle">
          Tampon {buffer === null ? "indisponible" : `${buffer.size} / ${buffer.capacity}`}
          {" · "}perdues navigateur {feed.browserDropped}
          {" · "}tronquées serveur {feed.truncated}
        </p>
        <p className="font-mono text-[0.6875rem] text-muted" role="status">
          {entries.length} trace{entries.length > 1 ? "s" : ""} affichée
          {entries.length > 1 ? "s" : ""}
        </p>
      </div>

      {entries.length === 0 ? (
        <p className="mt-4 border border-dashed border-border px-4 py-6 text-center text-sm text-subtle">
          {feed.entries.length === 0
            ? "Aucune Trace reçue pour l'instant."
            : "Aucune Trace ne correspond aux filtres."}
        </p>
      ) : (
        <ol
          role="log"
          aria-live="off"
          aria-label="Traces récentes"
          className="mt-3 max-h-[32rem] divide-y divide-border overflow-y-auto border border-border"
        >
          {entries.map((entry, index) => (
            <TraceRow
              key={`${entry.timestamp}-${entry.source}-${entry.kind}-${index}`}
              entry={entry}
            />
          ))}
        </ol>
      )}
    </Panel>
  );
}

function TraceRow({ entry }: { entry: TraceEntry }) {
  const payload = formatTracePayload(entry.payload);
  return (
    <li className="grid grid-cols-[auto_auto_minmax(0,1fr)] items-start gap-x-3 gap-y-1 px-3 py-2 text-xs odd:bg-surface-2/40">
      <time
        className="font-mono text-[0.6875rem] text-subtle"
        dateTime={new Date(entry.timestamp * 1000).toISOString()}
      >
        {formatTraceTime(entry.timestamp)}
      </time>
      <Badge tone={traceLevelTone(entry.level)}>
        {formatTraceLevel(entry.level)}
      </Badge>
      <div className="min-w-0">
        <p className="break-words font-mono text-text">
          <span className="text-muted">{entry.source}</span> · {entry.kind}
        </p>
        {payload ? (
          <details className="mt-1">
            <summary className="cursor-pointer font-mono text-[0.6875rem] text-subtle hover:text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
              Payload
            </summary>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words font-mono text-[0.6875rem] text-muted">
              {payload}
            </pre>
          </details>
        ) : null}
      </div>
    </li>
  );
}

function FilterSelect({
  label,
  value,
  options,
  render,
  onChange,
}: {
  label: string;
  value: string;
  options: readonly string[];
  render: (option: string) => string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
        {label}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 rounded-sm border border-border bg-canvas px-2 font-mono text-xs text-text transition-quiet hover:border-border-strong focus:border-accent"
      >
        <option value="">Tout</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {render(option)}
          </option>
        ))}
      </select>
    </label>
  );
}
