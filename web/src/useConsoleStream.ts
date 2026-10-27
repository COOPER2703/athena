import { useEffect, useRef, useState } from "react";

import { backoffDelay, connectionLabel, type ConnectionStatus } from "./connection";
import { applyStreamFrame, initialStreamState, type StreamState } from "./stream";

/** Ce que la page consomme du flux : état, log et état de la connexion. */
export interface ConsoleStream extends StreamState {
  connection: ConnectionStatus;
  connectionLabel: string;
}

/** Options d'ouverture du flux. */
export interface ConsoleStreamOptions {
  /**
   * Appelé quand la connexion se ferme **sans avoir jamais été ouverte** :
   * le handshake a été refusé (401) avant l'upgrade. La page en profite pour
   * vérifier le Ticket Console et revenir à l'écran de connexion.
   */
  onClosedBeforeOpen?: () => void;
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
export function useConsoleStream(
  options: ConsoleStreamOptions = {},
): ConsoleStream {
  const [stream, setStream] = useState<StreamState>(initialStreamState);
  const [connection, setConnection] = useState<ConnectionStatus>("connecting");
  // Le callback vit dans une ref : l'effet ne se réexécute pas à chaque rendu
  // du parent, et la connexion n'est ouverte qu'une fois.
  const closedBeforeOpen = useRef(options.onClosedBeforeOpen);
  closedBeforeOpen.current = options.onClosedBeforeOpen;

  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    let opened = false;

    const connect = () => {
      if (disposed) {
        return;
      }
      opened = false;
      setConnection(failures === 0 ? "connecting" : "reconnecting");
      const ws = new WebSocket(streamUrl());
      socket = ws;

      ws.addEventListener("open", () => {
        if (disposed) {
          return;
        }
        opened = true;
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
        // `applyStreamFrame` valide la forme : une trame malformée ou d'un type
        // inconnu rend le state inchangé, et React ne relance pas de rendu.
        setStream((state) => applyStreamFrame(state, parsed));
      });

      ws.addEventListener("close", () => {
        if (disposed) {
          return;
        }
        if (!opened) {
          closedBeforeOpen.current?.();
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
