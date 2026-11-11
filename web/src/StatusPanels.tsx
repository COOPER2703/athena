import {
  formatMarkerState,
  formatPluginState,
  instructionNotice,
  pluginSheet,
  pluginStateClass,
  type PluginInfo,
  type PluginMarker,
} from "./plugins";
import {
  clientDisplayName,
  isActiveClient,
  shortClientId,
  type ClientInfo,
} from "./session";
import type { ConsoleConfig } from "./status";

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

/** Un marqueur du diagnostic : état lisible, note ou manques nommés. */
function DiagnosticMarker({ marker }: { marker: PluginMarker }) {
  // Le manifeste invalide affiche déjà sa raison plus haut : on ne la répète
  // pas dans la liste des manques, la note du marqueur suffit.
  const gaps = marker.key === "manifest" ? [] : marker.gaps;
  return (
    <li className={`plugin__marker plugin__marker--${marker.state}`}>
      <span className="plugin__marker-label">{marker.label}</span>
      <span className="plugin__marker-state">
        {formatMarkerState(marker.state)}
      </span>
      {marker.note !== null ? (
        <p className="plugin__marker-note">{marker.note}</p>
      ) : null}
      {gaps.length > 0 ? (
        <ul className="plugin__gaps">
          {gaps.map((gap) => (
            <li className="plugin__gap mono" key={gap}>
              {gap}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/** Fiche dépliée d'un plugin : identité, réglages, diagnostic, Tools et panne. */
function PluginSheet({ plugin }: { plugin: PluginInfo }) {
  const sheet = pluginSheet(plugin);
  const notice = instructionNotice(plugin);
  // Un manifeste invalide porte la même raison dans `invalid_reason` et dans
  // `reason` : on ne l'affiche qu'une fois, sous l'onglet qui l'explique.
  const failure =
    sheet.failure !== null && sheet.failure !== sheet.invalidReason
      ? sheet.failure
      : null;

  return (
    <div className="plugin__sheet">
      {sheet.retry ? (
        <p className="notice notice--retry" role="status">
          Retente en cours : le serveur réessaiera le chargement.
        </p>
      ) : null}

      {sheet.declarationMissing ? (
        <p className="notice">
          Aucune Déclaration lue : le dossier n'a pas pu être inspecté.
        </p>
      ) : (
        <dl className="facts plugin__facts">
          {sheet.identity.map((fact) => (
            <div className="fact" key={fact.label}>
              <dt>{fact.label}</dt>
              <dd className={fact.mono ? "mono" : undefined}>{fact.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {sheet.invalidReason !== null ? (
        <div className="plugin__failure">
          <h3 className="plugin__section-title">Manifeste invalide</h3>
          <p className="plugin__message mono">{sheet.invalidReason}</p>
        </div>
      ) : null}

      {failure !== null ? (
        <div className="plugin__failure">
          <h3 className="plugin__section-title">Raison de l'échec</h3>
          <p className="plugin__message mono">{failure}</p>
        </div>
      ) : null}

      <div className="plugin__section">
        <h3 className="plugin__section-title">Configuration</h3>
        {sheet.config.length > 0 ? (
          <ul className="plugin__settings">
            {sheet.config.map((field) => (
              <li className="plugin__setting" key={field.key}>
                <span className="plugin__setting-key mono">{field.key}</span>
                <span className="plugin__setting-meta">
                  {field.type}
                  {field.required ? " · requis" : ""}
                </span>
                <span
                  className={`plugin__setting-value mono${
                    field.isDefault ? " plugin__setting-value--default" : ""
                  }`}
                >
                  {field.valueLabel}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="notice">
            {sheet.declarationMissing
              ? "Réglages inconnus : aucune Déclaration lue."
              : "Aucun champ de configuration déclaré."}
          </p>
        )}
      </div>

      <div className="plugin__section">
        <h3 className="plugin__section-title">Secrets</h3>
        {sheet.secrets.length > 0 ? (
          <ul className="plugin__settings">
            {sheet.secrets.map((secret) => (
              <li className="plugin__setting plugin__setting--secret" key={secret.key}>
                <span className="plugin__setting-key mono">{secret.key}</span>
                <span className="plugin__setting-meta">
                  {secret.required ? "requis" : "optionnel"}
                </span>
                <span className="plugin__setting-variable mono">
                  {secret.variable || "—"}
                </span>
                <span
                  className={`plugin__setting-presence plugin__setting-presence--${
                    secret.present ? "present" : "absent"
                  }`}
                >
                  {secret.presenceLabel}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="notice">
            {sheet.declarationMissing
              ? "Secrets inconnus : aucune Déclaration lue."
              : "Aucun secret déclaré."}
          </p>
        )}
      </div>

      <div className="plugin__section">
        <h3 className="plugin__section-title">Prêt à activer</h3>
        <ul className="plugin__diagnostic">
          <DiagnosticMarker marker={sheet.diagnostic.manifest} />
          <DiagnosticMarker marker={sheet.diagnostic.config} />
          <DiagnosticMarker marker={sheet.diagnostic.secrets} />
        </ul>
        {sheet.diagnostic.unknown ? (
          <p className="notice">
            Diagnostic partiel : des exigences restent illisibles.
          </p>
        ) : null}
      </div>

      <div className="plugin__section">
        <h3 className="plugin__section-title">Tools</h3>
        {sheet.tools.length > 0 ? (
          <ul className="plugin__tool-list">
            {sheet.tools.map((tool) => (
              <li className="plugin__tool mono" key={tool}>
                {tool}
              </li>
            ))}
          </ul>
        ) : (
          <p className="notice">Aucun Tool exposé.</p>
        )}
      </div>

      <div className="plugin__section">
        <h3 className="plugin__section-title">Instruction</h3>
        {notice !== null ? (
          <p className="notice">{notice}</p>
        ) : (
          <p className="plugin__instruction">{sheet.instruction}</p>
        )}
      </div>
    </div>
  );
}

/** Registre des Plugins connus : une ligne dépliable par plugin installé. */
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
        <p className="notice">Aucun Plugin installé.</p>
      ) : (
        <ul className="plugin-list">
          {plugins.map((plugin, index) => (
            <li className="plugin" key={plugin.name || `plugin-${index}`}>
              <details className="plugin__details">
                <summary className="plugin__head plugin__summary">
                  <span className="plugin__name">
                    {plugin.name || "(sans nom)"}
                  </span>
                  <span
                    className={`badge badge--plugin-${pluginStateClass(
                      plugin.state,
                    )}`}
                  >
                    {formatPluginState(plugin.state)}
                  </span>
                </summary>
                <PluginSheet plugin={plugin} />
              </details>
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
