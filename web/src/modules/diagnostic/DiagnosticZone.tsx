import { useQuery } from "@tanstack/react-query";

import { fetchStatus } from "../../api/client";
import { describeError } from "../../api/errors";
import { queryKeys } from "../../api/queryClient";
import { Notice } from "../../shared/ui/Notice";
import { HealthBanner } from "./HealthBanner";
import { normalizeSnapshot } from "./model";
import { PluginsPanel } from "./PluginsPanel";
import { ClientsPanel, SessionPanel } from "./SessionPanels";
import { TraceFeed } from "./TraceFeed";
import { useDiagnosticStream } from "./useDiagnosticStream";

/**
 * Diagnostic : santé, Session, Clients, Plugins et Traces.
 *
 * Deux lectures du même instantané : `GET /api/status` donne l'état au premier
 * rendu (et reste le repli si le flux tombe), le WebSocket `/api/stream` prend
 * ensuite le relais et pousse les changements. Le Diagnostic ne pilote rien :
 * il observe (ADR-0016).
 */
export function DiagnosticZone() {
  const stream = useDiagnosticStream();
  const query = useQuery({
    queryKey: queryKeys.status,
    queryFn: async () => normalizeSnapshot(await fetchStatus()),
    // Le direct fait foi dès qu'il est connecté ; sinon on réinterroge pour ne
    // pas laisser un bandeau figé après une coupure.
    refetchInterval: stream.connection === "connected" ? false : 15_000,
  });

  const snapshot = stream.snapshot ?? query.data;

  if (snapshot === undefined) {
    return (
      <div className="px-5 py-8 sm:px-8 lg:px-10 lg:py-12">
        {query.error ? (
          <Notice tone="danger" title="Instantané illisible">
            {describeError(query.error)}
          </Notice>
        ) : (
          <Notice tone="info" title="Lecture de l'état">
            La Console interroge le serveur et ouvre le flux d'observation…
          </Notice>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 px-5 py-8 sm:px-8 lg:px-10 lg:py-10">
      <HealthBanner
        snapshot={snapshot}
        connection={stream.connection}
        connectionLabel={stream.connectionLabel}
      />

      <div className="grid gap-6 xl:grid-cols-3">
        <SessionPanel snapshot={snapshot} />
        <ClientsPanel snapshot={snapshot} />
        <PluginsPanel snapshot={snapshot} />
      </div>

      <TraceFeed feed={stream.feed} buffer={snapshot.health.traceBuffer} />
    </div>
  );
}
