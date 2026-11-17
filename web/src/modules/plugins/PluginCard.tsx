import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { describeError } from "../../api/errors";
import {
  updatePlugin,
  type PluginEntry,
  type PluginUpdate,
} from "../../api/plugins";
import { queryKeys } from "../../api/queryClient";
import { cn } from "../../shared/cn";
import { Badge } from "../../shared/ui/Badge";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { PluginConfigForm } from "./PluginConfigForm";
import { PluginSecrets } from "./PluginSecrets";
import {
  RESTART_NOTICE,
  hasTools,
  platformLabel,
  pluginStateLabel,
  pluginStateTone,
} from "./plugins";

/**
 * Une carte de Plugin : état, activation, configuration et secrets.
 *
 * L'activation est un simple booléen persisté ; le serveur ne recharge rien,
 * donc l'écran rappelle le redémarrage après chaque écriture.
 */
export function PluginCard({ plugin }: { plugin: PluginEntry }) {
  const queryClient = useQueryClient();
  const [saved, setSaved] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (update: PluginUpdate) => updatePlugin(plugin.name, update),
    onSuccess: async () => {
      setSaved(RESTART_NOTICE);
      await queryClient.invalidateQueries({ queryKey: queryKeys.plugins });
    },
  });

  return (
    <section
      aria-labelledby={`plugin-${plugin.name}`}
      className={cn(
        "rounded-sm border bg-surface px-5 py-5 transition-quiet sm:px-6",
        plugin.state === "activated" ? "border-accent-line" : "border-border",
        plugin.state === "activated" && "halo-active",
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p
            id={`plugin-${plugin.name}`}
            className="font-mono text-sm tracking-tight text-text"
          >
            {plugin.name}
          </p>
          <p className="mt-1 text-sm leading-relaxed text-muted">
            {plugin.description || "Aucune description fournie."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={pluginStateTone(plugin.state)}>
            {pluginStateLabel(plugin.state)}
          </Badge>
          <Badge tone="neutral">{platformLabel(plugin.platform)}</Badge>
          {plugin.version ? (
            <Badge tone="neutral">v{plugin.version}</Badge>
          ) : null}
          {plugin.pendingRestart ? (
            <Badge tone="warning">Redémarrage requis</Badge>
          ) : null}
        </div>
      </header>

      {plugin.error ? (
        <Notice tone="warning" className="mt-4">
          {plugin.error}
        </Notice>
      ) : null}

      <dl className="mt-4 grid gap-x-6 gap-y-2 border-t border-border pt-4 text-xs sm:grid-cols-2">
        <div className="flex items-center justify-between gap-4">
          <dt className="font-mono uppercase tracking-[0.14em] text-subtle">
            Activation
          </dt>
          <dd className="text-muted">
            {plugin.enabled ? "Activé" : "Désactivé"}
          </dd>
        </div>
        <div className="flex items-center justify-between gap-4">
          <dt className="font-mono uppercase tracking-[0.14em] text-subtle">
            Tools exposés
          </dt>
          <dd className="font-mono text-text">
            {hasTools(plugin) ? plugin.tools.length : "aucun"}
          </dd>
        </div>
      </dl>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button
          variant={plugin.enabled ? "secondary" : "primary"}
          size="sm"
          disabled={mutation.isPending}
          onClick={() => {
            setSaved(null);
            mutation.mutate({ enabled: !plugin.enabled });
          }}
        >
          {mutation.isPending
            ? "Enregistrement…"
            : plugin.enabled
              ? "Désactiver"
              : "Activer"}
        </Button>
        <span className="font-mono text-[0.6875rem] uppercase tracking-[0.12em] text-subtle">
          {RESTART_NOTICE}
        </span>
      </div>

      {mutation.isError ? (
        <Notice tone="danger" className="mt-4">
          {describeError(mutation.error)}
        </Notice>
      ) : null}
      {saved !== null && !mutation.isError ? (
        <p role="status" className="mt-3 text-xs text-accent">
          {saved}
        </p>
      ) : null}

      {plugin.tools.length > 0 ? (
        <ul className="mt-4 flex flex-wrap gap-1.5" aria-label="Tools exposés">
          {plugin.tools.map((tool) => (
            <li key={tool}>
              <Badge tone="neutral">{tool}</Badge>
            </li>
          ))}
        </ul>
      ) : null}

      {plugin.instruction ? (
        <details className="mt-4 border-t border-border pt-4">
          <summary className="cursor-pointer font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
            Instruction d'usage
          </summary>
          <p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-muted">
            {plugin.instruction}
          </p>
        </details>
      ) : null}

      {plugin.config.length > 0 ? (
        <PluginConfigForm plugin={plugin} />
      ) : null}

      {plugin.secrets.length > 0 ? (
        <PluginSecrets plugin={plugin} />
      ) : null}
    </section>
  );
}
