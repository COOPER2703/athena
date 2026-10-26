import {
  clientDisplayName,
  isActiveClient,
  shortClientId,
  type ClientInfo,
} from "./session";
import {
  formatPluginState,
  pluginStateClass,
  type ConsoleConfig,
  type PluginInfo,
} from "./status";

/** Panneau des Clients branchés, avec le badge du Client actif. */
export function ClientsPanel({
  clients,
  activeClientId,
}: {
  clients: readonly ClientInfo[];
  activeClientId: string;
}) {
  return (
    <section className="panel" aria-labelledby="clients-title">
      <div className="panel__header">
        <h2 className="panel__title" id="clients-title">
          Clients
        </h2>
        <span className="badge">{clients.length}</span>
      </div>

      {clients.length === 0 ? (
        <p className="notice">Aucun Client branché.</p>
      ) : (
        <ul className="client-list">
          {clients.map((client) => {
            const active = isActiveClient(client.client_id, activeClientId);
            return (
              <li className="client" key={client.client_id}>
                <span className="client__name">
                  {clientDisplayName(client)}
                </span>
                <span className="client__platform">
                  {client.platform || "—"}
                </span>
                <span
                  className="client__id mono"
                  title={client.client_id}
                >
                  {shortClientId(client.client_id)}
                </span>
                {active ? (
                  <span className="badge badge--active">Client actif</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** Panneau des Plugins connus, de leur état et de leurs Tools exposés. */
export function PluginsPanel({ plugins }: { plugins: readonly PluginInfo[] }) {
  return (
    <section className="panel" aria-labelledby="plugins-title">
      <div className="panel__header">
        <h2 className="panel__title" id="plugins-title">
          Plugins
        </h2>
        <span className="badge">{plugins.length}</span>
      </div>

      {plugins.length === 0 ? (
        <p className="notice">Aucun Plugin chargé.</p>
      ) : (
        <ul className="plugin-list">
          {plugins.map((plugin) => (
            <li className="plugin" key={plugin.name}>
              <div className="plugin__head">
                <span className="plugin__name">{plugin.name}</span>
                <span
                  className={`badge badge--plugin-${pluginStateClass(
                    plugin.state,
                  )}`}
                >
                  {formatPluginState(plugin.state)}
                </span>
              </div>
              <span className="plugin__tools mono">
                {plugin.tools.length > 0
                  ? plugin.tools.join(", ")
                  : "Aucun Tool"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Configuration non sensible : liste blanche explicite, aucun secret. */
const CONFIG_FIELDS: ReadonlyArray<readonly [string, string, boolean]> = [
  ["Modèle Live", "model", false],
  ["Voix", "voice", false],
  ["Utilisateur", "user_id", false],
  ["Mémoire", "memory_db", true],
  ["Journaux", "log_dir", true],
  ["Version", "version", false],
];

export function ConfigPanel({ config }: { config: ConsoleConfig }) {
  return (
    <section className="panel" aria-labelledby="config-title">
      <div className="panel__header">
        <h2 className="panel__title" id="config-title">
          Configuration
        </h2>
      </div>

      <dl className="facts">
        {CONFIG_FIELDS.map(([label, key, mono]) => (
          <div className="fact" key={key}>
            <dt>{label}</dt>
            <dd className={mono ? "mono" : undefined}>{config[key] || "—"}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
