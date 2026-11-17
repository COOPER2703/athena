import {
  formatBytes,
  formatDate,
  formatDuration,
  pluralize,
} from "../../shared/format";
import { Badge } from "../../shared/ui/Badge";
import { Notice } from "../../shared/ui/Notice";
import {
  DATABASE_LABELS,
  DATABASE_NAMES,
  SURFACE_LABELS,
  SURFACE_NAMES,
  installationIssues,
  type DiagnosticSnapshot,
  type HealthInfo,
} from "./model";
import { Panel } from "./Panel";
import { connectionTone, type ConnectionStatus } from "./stream";

function consolidationValue(health: HealthInfo): string {
  const consolidation = health.lastConsolidation;
  if (consolidation === null) {
    return "Aucune";
  }
  const when = formatDate(consolidation.at);
  const facts = pluralize(
    consolidation.factsStored,
    "fait retenu",
    "faits retenus",
  );
  return `${when} · ${facts}`;
}

function lastErrorValue(health: HealthInfo): string {
  const error = health.lastError;
  if (error === null) {
    return "Aucune";
  }
  return `${formatDate(error.timestamp)} · ${error.source} · ${error.kind}`;
}

function bufferValue(health: HealthInfo): string {
  const buffer = health.traceBuffer;
  if (buffer === null) {
    return "Indisponible";
  }
  return `${buffer.size} / ${buffer.capacity}`;
}

function surfacesValue(snapshot: DiagnosticSnapshot): string {
  const open = SURFACE_NAMES.filter(
    (name) => snapshot.installation.surfaces[name],
  ).map((name) => SURFACE_LABELS[name]);
  return open.length > 0 ? open.join(" · ") : "Toutes fermées";
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="font-mono text-[0.625rem] uppercase tracking-[0.12em] text-subtle">
        {label}
      </dt>
      <dd className="mt-1 truncate font-mono text-sm text-text" title={value}>
        {value}
      </dd>
    </div>
  );
}

function Metrics({
  items,
  className,
}: {
  items: readonly { label: string; value: string }[];
  className?: string;
}) {
  return (
    <dl className={className}>
      {items.map((item) => (
        <Metric key={item.label} label={item.label} value={item.value} />
      ))}
    </dl>
  );
}

export interface HealthBannerProps {
  snapshot: DiagnosticSnapshot;
  connection: ConnectionStatus;
  connectionLabel: string;
}

/**
 * Bandeau de santé : l'état de l'instance en un regard.
 *
 * Version, uptime, dernière Consolidation, tailles des bases, borne du Tampon
 * de traces et dernière erreur. Les manques d'Installation ne masquent rien :
 * ils s'ajoutent en bandeaux explicites, sous les mesures.
 */
export function HealthBanner({
  snapshot,
  connection,
  connectionLabel,
}: HealthBannerProps) {
  const { health } = snapshot;
  const issues = installationIssues(snapshot);

  return (
    <Panel
      title="Santé de l'instance"
      subtitle="Mesures vivantes du processus et de ses bases."
      actions={<Badge tone={connectionTone(connection)}>{connectionLabel}</Badge>}
    >
      <Metrics
        className="grid gap-x-6 gap-y-3 sm:grid-cols-2 xl:grid-cols-4"
        items={[
          { label: "Version", value: health.version || "—" },
          {
            label: "Uptime",
            value:
              health.uptimeSeconds === null
                ? "—"
                : formatDuration(health.uptimeSeconds),
          },
          {
            label: "Dernière Consolidation",
            value: consolidationValue(health),
          },
          { label: "Dernière erreur", value: lastErrorValue(health) },
        ]}
      />

      <Metrics
        className="mt-4 grid gap-x-6 gap-y-3 border-t border-border pt-4 sm:grid-cols-2 xl:grid-cols-4"
        items={[
          ...DATABASE_NAMES.map((name) => ({
            label: DATABASE_LABELS[name] ?? name,
            value: formatBytes(health.databases[name] ?? 0),
          })),
          { label: "Tampon de traces", value: bufferValue(health) },
        ]}
      />

      <Metrics
        className="mt-4 grid gap-x-6 gap-y-3 border-t border-border pt-4 sm:grid-cols-2"
        items={[
          { label: "Surfaces ouvertes", value: surfacesValue(snapshot) },
          {
            label: "Secrets",
            value: snapshot.installation.secretsUnreadable
              ? "Illisibles"
              : "Lisibles",
          },
        ]}
      />

      {issues.length > 0 ? (
        <div className="mt-4 flex flex-col gap-2 border-t border-border pt-4">
          {issues.map((issue) => (
            <Notice key={issue.title} tone={issue.tone} title={issue.title}>
              {issue.body}
            </Notice>
          ))}
        </div>
      ) : null}

      {health.lastError !== null ? (
        <div className="mt-4">
          <Notice tone="danger" title="Dernière erreur">
            <span className="font-mono text-xs">
              {formatDate(health.lastError.timestamp)} · {health.lastError.source}{" "}
              · {health.lastError.kind}
            </span>
          </Notice>
        </div>
      ) : null}
    </Panel>
  );
}
