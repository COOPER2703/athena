import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { describeError } from "../../api/errors";
import { fetchPlugins } from "../../api/plugins";
import { queryKeys } from "../../api/queryClient";
import { PluginsIcon } from "../../shared/icons";
import { Button } from "../../shared/ui/Button";
import { EmptyState } from "../../shared/ui/EmptyState";
import { Notice } from "../../shared/ui/Notice";
import { DiscoverTab } from "./DiscoverTab";
import { PluginCard } from "./PluginCard";
import { TrustPanel } from "./TrustPanel";

type Tab = "installed" | "discover";

const TAB_BASE =
  "rounded-sm border px-3 py-1.5 text-sm transition-quiet focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

/**
 * Zone Plugins : les Plugins installés, puis la place réservée au store.
 *
 * Aucune installation n'est possible : le store est hors périmètre (ADR-0014).
 * Toutes les écritures sont persistées et prennent effet au redémarrage ; la
 * zone le dit à chaque étape plutôt que de laisser croire à un rechargement.
 */
export function PluginsZone() {
  const [tab, setTab] = useState<Tab>("installed");
  const plugins = useQuery({
    queryKey: queryKeys.plugins,
    queryFn: () => fetchPlugins(),
  });

  return (
    <div className="px-5 py-8 sm:px-8 lg:px-10 lg:py-12">
      <div
        role="tablist"
        aria-label="Plugins"
        className="mb-6 inline-flex gap-2 rounded-sm border border-border bg-surface p-1"
      >
        <button
          type="button"
          role="tab"
          id="plugins-tab-installed"
          aria-selected={tab === "installed"}
          aria-controls="plugins-panel-installed"
          onClick={() => setTab("installed")}
          className={
            tab === "installed"
              ? `${TAB_BASE} border-accent-line bg-accent-soft text-accent`
              : `${TAB_BASE} border-transparent text-muted hover:text-text`
          }
        >
          Installés
        </button>
        <button
          type="button"
          role="tab"
          id="plugins-tab-discover"
          aria-selected={tab === "discover"}
          aria-controls="plugins-panel-discover"
          onClick={() => setTab("discover")}
          className={
            tab === "discover"
              ? `${TAB_BASE} border-accent-line bg-accent-soft text-accent`
              : `${TAB_BASE} border-transparent text-muted hover:text-text`
          }
        >
          Découvrir
        </button>
      </div>

      {tab === "discover" ? (
        <div role="tabpanel" id="plugins-panel-discover" aria-labelledby="plugins-tab-discover">
          <DiscoverTab />
        </div>
      ) : (
        <div
          role="tabpanel"
          id="plugins-panel-installed"
          aria-labelledby="plugins-tab-installed"
          className="flex flex-col gap-5"
        >
          <TrustPanel />

          {plugins.isPending ? (
            <p
              role="status"
              className="rounded-sm border border-border bg-surface px-4 py-3 text-sm text-muted"
            >
              Chargement des Plugins…
            </p>
          ) : null}

          {plugins.isError ? (
            <Notice tone="danger" title="Plugins indisponibles">
              <p>{describeError(plugins.error)}</p>
              <Button
                variant="secondary"
                size="sm"
                className="mt-3"
                onClick={() => {
                  void plugins.refetch();
                }}
              >
                Réessayer
              </Button>
            </Notice>
          ) : null}

          {plugins.data?.restartRequired ? (
            <Notice tone="warning" title="Redémarrage requis">
              Au moins un changement d'activation ou de configuration attend un
              redémarrage du serveur. Rien n'est rechargé à chaud.
            </Notice>
          ) : null}

          {plugins.data !== undefined && plugins.data.plugins.length === 0 ? (
            <EmptyState
              icon={<PluginsIcon className="h-5 w-5" />}
              title="Aucun Plugin installé"
              body="Déposez un dossier de Plugin dans le répertoire des Plugins du serveur, puis redémarrez pour le voir ici."
              hint="L'installation depuis la Console n'existe pas encore."
            />
          ) : null}

          {plugins.data?.plugins.map((plugin) => (
            <PluginCard key={plugin.name} plugin={plugin} />
          ))}
        </div>
      )}
    </div>
  );
}
