import { PluginsIcon } from "../../shared/icons";
import { Badge } from "../../shared/ui/Badge";
import { EmptyState } from "../../shared/ui/EmptyState";
import { formatPluginState, pluginStateTone } from "./model";
import type { DiagnosticSnapshot } from "./model";
import { Panel } from "./Panel";

/**
 * Plugins observés : état de chargement et Tools réellement exposés.
 *
 * Le Diagnostic ne configure rien : la version, la plateforme et l'activation
 * appartiennent à la zone Plugins (#176). Ici on répond seulement « qu'est-ce
 * qu'Athena sait faire, là, maintenant ? ».
 */
export function PluginsPanel({ snapshot }: { snapshot: DiagnosticSnapshot }) {
  const { plugins } = snapshot;
  const active = plugins.filter((plugin) => plugin.state === "activated").length;

  return (
    <Panel
      title="Plugins"
      subtitle="Ce qu'Athena peut appeler pendant un Tour."
      actions={<Badge>{`${active} / ${plugins.length}`}</Badge>}
    >
      {plugins.length === 0 ? (
        <EmptyState
          icon={<PluginsIcon className="h-5 w-5" />}
          title="Aucun Plugin chargé"
          body="Un Plugin activé apparaît ici avec son état et les Tools qu'il ajoute au modèle."
          hint="L'activation se fait dans la zone Plugins."
          className="border-0 bg-transparent px-0 py-2 sm:px-0 sm:py-2"
        />
      ) : (
        <ul className="flex flex-col divide-y divide-border">
          {plugins.map((plugin) => (
            <li key={plugin.name} className="py-2 first:pt-0 last:pb-0">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="truncate text-sm text-text">{plugin.name}</p>
                <Badge tone={pluginStateTone(plugin.state)}>
                  {formatPluginState(plugin.state)}
                </Badge>
              </div>
              {plugin.tools.length > 0 ? (
                <p className="mt-1 break-words font-mono text-[0.6875rem] text-subtle">
                  {plugin.tools.join(" · ")}
                </p>
              ) : (
                <p className="mt-1 font-mono text-[0.6875rem] text-subtle">
                  Aucun Tool exposé
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
