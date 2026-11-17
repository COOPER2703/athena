import { useEffect, useState } from "react";

import {
  applyTrace,
  backoffDelay,
  connectionLabel,
  parseStreamFrame,
  resetOnHello,
  type ConnectionStatus,
  type FeedState,
  emptyFeed,
} from "./stream";
import { normalizeSnapshot, type DiagnosticSnapshot } from "./model";

/** Ce que le Diagnostic consomme du flux : état, traces et connexion. */
export interface DiagnosticStream {
  /** Dernier instantané reçu par le flux, ou `null` avant le `hello`. */
  snapshot: DiagnosticSnapshot | null;
  feed: FeedState;
  connection: ConnectionStatus;
  connectionLabel: string;
}

/** URL du flux, sur la même origine que la page servie par la Console. */
function streamUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/stream`;
}

/**
 * Connexion unique du Diagnostic à `GET /api/stream`.
 *
 * La reconnexion est automatique, avec un backoff exponentiel. Chaque `hello`
 * remplace l'instantané **et** le backlog : après un redémarrage du serveur, la
 * page se resynchronise seule, sans rechargement. Une trame `status` ne touche
 * pas aux traces ; une trame `trace` ne touche pas à l'instantané.
 *
 * Le repli HTTP (`GET /api/status`) reste la responsabilité de l'appelant :
 * ce hook ne fait qu'observer le direct, et laisse `snapshot` à `null` tant que
 * la connexion n'a pas abouti.
 */
export function useDiagnosticStream(): DiagnosticStream {
  const [snapshot, setSnapshot] = useState<DiagnosticSnapshot | null>(null);
  const [feed, setFeed] = useState<FeedState>(emptyFeed);
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");

  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;

    const connect = () => {
      if (disposed) {
        return;
      }
      setConnection(failures === 0 ? "connecting" : "reconnecting");
      const ws = new WebSocket(streamUrl());
      socket = ws;

      ws.addEventListener("open", () => {
        if (disposed) {
          return;
        }
        failures = 0;
        setConnection("connected");
      });

      ws.addEventListener("message", (event) => {
        if (disposed) {
          return;
        }
        let parsed: unknown;
        try {
          parsed = JSON.parse(String(event.data));
        } catch {
          return;
        }
        const frame = parseStreamFrame(parsed);
        if (frame === null) {
          return;
        }
        if (frame.type === "hello") {
          setSnapshot(normalizeSnapshot(frame.data.snapshot));
          setFeed((state) => resetOnHello(state, frame));
          return;
        }
        if (frame.type === "status") {
          setSnapshot(normalizeSnapshot(frame.data));
          return;
        }
        setFeed((state) => applyTrace(state, frame.data));
      });

      ws.addEventListener("close", () => {
        if (disposed) {
          return;
        }
        if (typeof navigator !== "undefined" && !navigator.onLine) {
          setConnection("disconnected");
          return;
        }
        setConnection("reconnecting");
        timer = setTimeout(connect, backoffDelay(failures + 1));
        failures += 1;
      });

      ws.addEventListener("error", () => {
        // `close` suit toujours : c'est là que la reconnexion est planifiée.
        ws.close();
      });
    };

    const handleOffline = () => {
      setConnection("disconnected");
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

  return {
    snapshot,
    feed,
    connection,
    connectionLabel: connectionLabel(connection),
  };
}
