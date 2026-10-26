import { useEffect, useState } from "react";

import { fetchStatus } from "./api";
import { EventForm } from "./EventForm";
import { sessionSummary } from "./session";
import { ClientsPanel, ConfigPanel, PluginsPanel } from "./StatusPanels";
import type { StatusData } from "./status";
import { TraceFeed } from "./TraceFeed";
import { useConsoleStream } from "./useConsoleStream";

/**
 * Page unique de la Console : état vivant, panneaux et flux de traces.
 *
 * L'état vient du WebSocket (`hello` puis trames `status`) ; `GET /api/status`
 * sert de repli initial et le bouton « Actualiser » le relit à la demande.
 * Une reconnexion remplace l'état et le log par le `hello` courant.
 */
export function App() {
  const stream = useConsoleStream();
  const [fetched, setFetched] = useState<StatusData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    fetchStatus(controller.signal)
      .then((status) => setFetched(status))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        const detail =
          reason instanceof Error ? reason.message : "erreur réseau inconnue";
        setError(
          `Impossible de joindre l'API de la Console (${detail}).`,
        );
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
          <p className="app__subtitle">Console web · surface d'observation</p>
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
                <dd className="mono">
                  {status.session.connection_id || "—"}
                </dd>
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

        <EventForm />
      </main>

      <footer className="app__footer">
        État poussé par <code>GET /api/stream</code> (repli{" "}
        <code>GET /api/status</code>), Événements via{" "}
        <code>POST /api/events</code>
      </footer>
    </div>
  );
}
