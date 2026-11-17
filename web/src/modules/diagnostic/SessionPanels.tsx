import { BrandMark } from "../../shared/icons";
import { Badge } from "../../shared/ui/Badge";
import { EmptyState } from "../../shared/ui/EmptyState";
import { formatSessionState, sessionSummary, sessionStateTone } from "./model";
import type { DiagnosticSnapshot } from "./model";
import { Facts, Panel } from "./Panel";

/**
 * Session du noyau et Client propriétaire.
 *
 * Aucune Session n'existe hors d'une conversation : « Inactive » est l'état
 * normal, et un Tour texte ne crée jamais de Session (ADR-0023).
 */
export function SessionPanel({ snapshot }: { snapshot: DiagnosticSnapshot }) {
  const summary = sessionSummary(snapshot.session, snapshot.clients);
  const config = snapshot.config;

  return (
    <Panel
      title="Session"
      subtitle="Qui parle à Athena, et dans quel état."
      actions={
        <Badge tone={sessionStateTone(snapshot.session.state)}>
          {summary.stateLabel}
        </Badge>
      }
    >
      <Facts
        items={[
          { label: "État", value: formatSessionState(snapshot.session.state) },
          { label: "Session", value: summary.sessionId },
          {
            label: "Connexion Live",
            value: snapshot.session.connectionId || "—",
          },
          { label: "Client propriétaire", value: summary.activeClient },
        ]}
      />

      <div className="mt-4 border-t border-border pt-4">
        <p className="mb-2 font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
          Instance
        </p>
        <Facts
          items={[
            { label: "Modèle", value: config.model || "—" },
            { label: "Voix", value: config.voice || "—" },
            { label: "Propriétaire", value: config.user_id || "—" },
          ]}
        />
      </div>
    </Panel>
  );
}

/** Clients branchés au transport, avec le propriétaire de la Session mis en avant. */
export function ClientsPanel({ snapshot }: { snapshot: DiagnosticSnapshot }) {
  const { clients, session } = snapshot;

  return (
    <Panel
      title="Clients connectés"
      subtitle="Les postes qui portent l'audio d'Athena."
      actions={<Badge>{clients.length}</Badge>}
    >
      {clients.length === 0 ? (
        <EmptyState
          icon={<BrandMark className="h-5 w-5" />}
          title="Aucun Client connecté"
          body="Un poste apparaît ici dès qu'il s'enregistre sur le transport avec un Jeton d'accès."
          hint="Le Client propriétaire n'existe que pendant une Session visible."
          className="border-0 bg-transparent px-0 py-2 sm:px-0 sm:py-2"
        />
      ) : (
        <ul className="flex flex-col divide-y divide-border">
          {clients.map((client) => {
            const owner = client.clientId === session.activeClientId;
            return (
              <li
                key={client.clientId}
                className="flex flex-wrap items-center justify-between gap-2 py-2 first:pt-0 last:pb-0"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm text-text">
                    {client.clientName || client.clientId}
                  </p>
                  <p
                    className="truncate font-mono text-[0.6875rem] text-subtle"
                    title={client.clientId}
                  >
                    {client.platform || "plateforme inconnue"} ·{" "}
                    {client.clientId}
                  </p>
                </div>
                {owner ? <Badge tone="accent">Propriétaire</Badge> : null}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
