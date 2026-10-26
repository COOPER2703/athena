import { useEffect, useState } from "react";

import { fetchStatus, type StatusPayload } from "./api";
import { sessionSummary } from "./session";
import { TraceFeed } from "./TraceFeed";

/** État de chargement de la page, sans état intermédiaire ambigu. */
type View =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; status: StatusPayload };

/** Page unique de la Console : le panneau Session lu depuis l'API. */
export function App() {
  const [view, setView] = useState<View>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setView({ kind: "loading" });
    fetchStatus(controller.signal)
      .then((status) => setView({ kind: "ready", status }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        const detail =
          error instanceof Error ? error.message : "erreur réseau inconnue";
        setView({
          kind: "error",
          message: `Impossible de joindre l'API de la Console (${detail}).`,
        });
      });
    return () => controller.abort();
  }, [attempt]);

  const summary =
    view.kind === "ready"
      ? sessionSummary(view.status.session, view.status.clients)
      : null;

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

          {view.kind === "loading" ? (
            <p className="notice">Chargement de l'état…</p>
          ) : null}
          {view.kind === "error" ? (
            <p className="notice notice--error" role="alert">
              {view.message}
            </p>
          ) : null}
          {summary !== null ? (
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
                  {view.kind === "ready" && view.status.session.connection_id
                    ? view.status.session.connection_id
                    : "—"}
                </dd>
              </div>
              <div className="fact">
                <dt>Client actif</dt>
                <dd>{summary.activeClient}</dd>
              </div>
            </dl>
          ) : null}
        </section>

        <TraceFeed />
      </main>

      <footer className="app__footer">
        Instantané lu depuis <code>GET /api/status</code>, traces depuis{" "}
        <code>GET /api/stream</code>
      </footer>
    </div>
  );
}
