import { useEffect, useRef, useState } from "react";

import { fetchSession, fetchStatus } from "./api";
import { isUnauthorized } from "./auth";
import { EventForm } from "./EventForm";
import { sessionSummary } from "./session";
import { ClientsPanel, ConfigPanel, PluginsPanel } from "./StatusPanels";
import type { StatusData } from "./status";
import { TraceFeed } from "./TraceFeed";
import { useConsoleStream } from "./useConsoleStream";

interface ConsoleScreenProps {
  /** Nom du Compte connecté, affiché dans l'en-tête. */
  username: string;
  /** Appelé au premier 401 : Ticket Console expiré ou révoqué. */
  onUnauthorized: () => void;
}

/**
 * Console authentifiée : état vivant, panneaux, flux de traces et formulaire.
 *
 * Ce composant possède la connexion WebSocket : il n'est monté qu'avec un
 * Ticket Console valide, donc aucun flux n'est ouvert depuis l'écran de
 * connexion. Tout `401` (REST, formulaire ou handshake refusé avant
 * l'ouverture) remonte à `App` par `onUnauthorized`, qui ferme cet écran —
 * donc le flux — et revient à la connexion.
 */
export function ConsoleScreen({ username, onUnauthorized }: ConsoleScreenProps) {
  const [fetched, setFetched] = useState<StatusData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  const checkingSession = useRef(false);

  // Une fermeture du flux **avant** la première ouverture est un handshake
  // refusé (401) : on redemande l'état de session pour ne pas boucler.
  const stream = useConsoleStream({
    onClosedBeforeOpen: () => {
      if (checkingSession.current) {
        return;
      }
      checkingSession.current = true;
      fetchSession()
        .then((session) => {
          if (!session.authenticated) {
            unauthorized.current();
          }
        })
        .catch(() => {
          // Panne réseau : la reconnexion automatique reprend son cours.
        })
        .finally(() => {
          checkingSession.current = false;
        });
    },
  });

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    fetchStatus(controller.signal)
      .then((status) => setFetched(status))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        if (isUnauthorized(reason)) {
          unauthorized.current();
          return;
        }
        const detail =
          reason instanceof Error ? reason.message : "erreur réseau inconnue";
        setError(`Impossible de joindre l'API de la Console (${detail}).`);
      });
    return () => controller.abort();
  }, [attempt]);

  // Les trames `status` priment : elles sont plus récentes que le repli REST.
  const status = stream.status ?? fetched;
  const summary =
    status !== null ? sessionSummary(status.session, status.clients) : null;

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1 className="app__title">Athena</h1>
          <p className="app__subtitle">
            Console web · surface d'observation · Compte{" "}
            <span className="mono">{username}</span>
          </p>
        </div>
        <button
          className="button"
          type="button"
          onClick={() => setAttempt((value) => value + 1)}
        >
          Actualiser
        </button>
      </header>

      <main className="app__main">
        <section className="panel" aria-labelledby="session-title">
          <div className="panel__header">
            <h2 className="panel__title" id="session-title">
              Session
            </h2>
            {summary !== null ? (
              <span className="badge">{summary.stateLabel}</span>
            ) : null}
          </div>

          {status === null && error === null ? (
            <p className="notice">Chargement de l'état…</p>
          ) : null}
          {status === null && error !== null ? (
            <p className="notice notice--error" role="alert">
              {error}
            </p>
          ) : null}
          {summary !== null && status !== null ? (
            <dl className="facts">
              <div className="fact">
                <dt>État</dt>
                <dd>{summary.stateLabel}</dd>
              </div>
              <div className="fact">
                <dt>Session</dt>
                <dd className="mono">{summary.sessionId}</dd>
              </div>
              <div className="fact">
                <dt>Connexion</dt>
                <dd className="mono">{status.session.connection_id || "—"}</dd>
              </div>
              <div className="fact">
                <dt>Client actif</dt>
                <dd>{summary.activeClient}</dd>
              </div>
            </dl>
          ) : null}
        </section>

        {status !== null ? (
          <div className="app__panels">
            <ClientsPanel
              clients={status.clients}
              activeClientId={status.session.active_client_id}
            />
            <PluginsPanel plugins={status.plugins} />
          </div>
        ) : null}

        {status !== null ? <ConfigPanel config={status.config} /> : null}

        <TraceFeed feed={stream.feed} connection={stream.connection} />

        <EventForm onUnauthorized={onUnauthorized} />
      </main>

      <footer className="app__footer">
        État poussé par <code>GET /api/stream</code> (repli{" "}
        <code>GET /api/status</code>), Événements via{" "}
        <code>POST /api/events</code>
      </footer>
    </div>
  );
}
