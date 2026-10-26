import { useEffect, useState } from "react";

import { applyStreamFrame, initialStreamState, type StreamState } from "./stream";
import {
  backoffDelay,
  connectionLabel,
  type ConnectionStatus,
  type StreamFrame,
} from "./trace";

/** Ce que la page consomme du flux : état, log et état de la connexion. */
export interface ConsoleStream extends StreamState {
  connection: ConnectionStatus;
  connectionLabel: string;
}

/** URL du flux vivant, sur la même origine que la page servie par la Console. */
function streamUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/stream`;
}

/**
 * Connexion unique de la page à `GET /api/stream`.
 *
 * La reconnexion est automatique avec backoff exponentiel. Chaque `hello`
 * remplace l'état et le log (`applyStreamFrame`) : après un redémarrage du
 * serveur, la page se resynchronise seule sans rechargement. Une trame `status`
 * met à jour la Session et les panneaux sans toucher au flux de traces.
 */
export function useConsoleStream(): ConsoleStream {
  const [stream, setStream] = useState<StreamState>(initialStreamState);
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
        if (typeof parsed !== "object" || parsed === null) {
          return;
        }
        const frame = parsed as StreamFrame;
        setStream((state) => applyStreamFrame(state, frame));
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

  return { ...stream, connection, connectionLabel: connectionLabel(connection) };
}
