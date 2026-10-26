import { useEffect, useRef, useState } from "react";

import {
  applyTrace,
  backoffDelay,
  connectionLabel,
  emptyFeed,
  formatPayload,
  formatTimestamp,
  resetOnHello,
  type ConnectionStatus,
  type FeedState,
  type StreamFrame,
  type TraceEntry,
} from "./trace";

/** URL du flux vivant, sur la même origine que la page servie par la Console. */
function streamUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/stream`;
}

/**
 * Panneau de traces alimenté par `GET /api/stream`.
 *
 * La reconnexion est automatique avec backoff exponentiel. Chaque `hello`
 * remplace le backlog affiché (`resetOnHello`) : on ne concatène jamais deux
 * connexions. L'autoscroll se suspend d'un clic pour lire un passage stable.
 */
export function TraceFeed() {
  const [feed, setFeed] = useState<FeedState>(emptyFeed);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [autoScroll, setAutoScroll] = useState(true);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;

    const connect = () => {
      if (disposed) {
        return;
      }
      setStatus(failures === 0 ? "connecting" : "reconnecting");
      const ws = new WebSocket(streamUrl());
      socket = ws;

      ws.addEventListener("open", () => {
        if (disposed) {
          return;
        }
        failures = 0;
        setStatus("connected");
      });

      ws.addEventListener("message", (event) => {
        if (disposed) {
          return;
        }
        let frame: StreamFrame;
        try {
          frame = JSON.parse(String(event.data)) as StreamFrame;
        } catch {
          return;
        }
        if (frame.type === "hello") {
          setFeed((state) => resetOnHello(state, frame));
        } else if (frame.type === "trace") {
          setFeed((state) => applyTrace(state, frame.data));
        }
      });

      ws.addEventListener("close", () => {
        if (disposed) {
          return;
        }
        if (typeof navigator !== "undefined" && !navigator.onLine) {
          setStatus("disconnected");
          return;
        }
        setStatus("reconnecting");
        const delay = backoffDelay(failures);
        failures += 1;
        timer = setTimeout(connect, delay);
      });

      ws.addEventListener("error", () => {
        // `close` suit toujours : la reconnexion est planifiée là-bas.
        ws.close();
      });
    };

    const handleOffline = () => {
      setStatus("disconnected");
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      socket?.close();
    };

    const handleOnline = () => {
      if (
        socket !== null &&
        (socket.readyState === WebSocket.CONNECTING ||
          socket.readyState === WebSocket.OPEN ||
          socket.readyState === WebSocket.CLOSING)
      ) {
        return;
      }
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      failures = 0;
      connect();
    };

    window.addEventListener("offline", handleOffline);
    window.addEventListener("online", handleOnline);
    connect();
    return () => {
      disposed = true;
      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("online", handleOnline);
      if (timer !== null) {
        clearTimeout(timer);
      }
      socket?.close();
    };
  }, []);

  useEffect(() => {
    if (!autoScroll) {
      return;
    }
    const list = scrollRef.current;
    if (list !== null) {
      list.scrollTop = list.scrollHeight;
    }
  }, [feed, autoScroll]);

  return (
    <section className="panel trace-panel" aria-labelledby="traces-title">
      <div className="panel__header">
        <h2 className="panel__title" id="traces-title">
          Traces
        </h2>
        <div className="trace-panel__controls">
          <span className={`status status--${status}`}>{connectionLabel(status)}</span>
          <button
            className="button"
            type="button"
            aria-pressed={autoScroll}
            onClick={() => setAutoScroll((value) => !value)}
          >
            {autoScroll ? "Défilement auto" : "Défilement suspendu"}
          </button>
        </div>
      </div>

      {feed.truncated > 0 ? (
        <p className="notice notice--warning" role="status">
          Flux tronqué : {feed.truncated} entrée(s) perdue(s).
        </p>
      ) : null}

      <div className="trace-list" ref={scrollRef} aria-live="polite">
        {feed.entries.length === 0 ? (
          <p className="notice">En attente de traces…</p>
        ) : (
          feed.entries.map((entry, index) => (
            <TraceRow key={`${entry.timestamp}-${index}`} entry={entry} />
          ))
        )}
      </div>
    </section>
  );
}

function levelClass(level: string): string {
  return level.toLowerCase().replace(/[^a-z]/g, "");
}

function TraceRow({ entry }: { entry: TraceEntry }) {
  const payload = formatPayload(entry.payload);
  return (
    <article className={`trace trace--${levelClass(entry.level)}`}>
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
