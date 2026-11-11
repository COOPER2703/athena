import { useState } from "react";

import {
  EMPTY_PLUGIN_FILTER,
  PLUGIN_STATE_FILTERS,
  filterPlugins,
  pluginCounterItems,
  pluginCounters,
  pluginResultSummary,
  pluginRow,
  pluginSheet,
  type PluginFilter,
  type PluginInfo,
  type PluginMarkerView,
  type PluginSheet,
} from "./plugins";
import "./registry.css";

/** Un marqueur du diagnostic : état lisible, note ou manques nommés. */
function DiagnosticMarker({ marker }: { marker: PluginMarkerView }) {
  return (
    <li className={`plugin__marker plugin__marker--${marker.stateClass}`}>
      <span className="plugin__marker-label">{marker.label}</span>
      <span className="plugin__marker-state">
        {marker.stateLabel}
      </span>
      {marker.note !== null ? (
        <p className="plugin__marker-note">{marker.note}</p>
      ) : null}
      {marker.gaps.length > 0 ? (
        <ul className="plugin__gaps">
          {marker.gaps.map((gap) => (
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
function PluginSheetView({ plugin }: { plugin: PluginInfo }) {
  const sheet: PluginSheet = pluginSheet(plugin);

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

      {sheet.failureMessage !== null ? (
        <div className="plugin__failure">
          <h3 className="plugin__section-title">Raison de l'échec</h3>
          <p className="plugin__message mono">{sheet.failureMessage}</p>
        </div>
      ) : null}

      <div className="plugin__section">
        <h3 className="plugin__section-title">Configuration</h3>
        {sheet.configNotice !== null ? (
          <p className="notice">{sheet.configNotice}</p>
        ) : (
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
        )}
      </div>

      <div className="plugin__section">
        <h3 className="plugin__section-title">Secrets</h3>
        {sheet.secretsNotice !== null ? (
          <p className="notice">{sheet.secretsNotice}</p>
        ) : (
          <ul className="plugin__settings">
            {sheet.secrets.map((secret) => (
              <li
                className="plugin__setting plugin__setting--secret"
                key={secret.key}
              >
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
        )}
      </div>

      <div className="plugin__section">
        <h3 className="plugin__section-title">Prêt à activer</h3>
        <ul className="plugin__diagnostic">
          {sheet.markers.map((marker) => (
            <DiagnosticMarker key={marker.key} marker={marker} />
          ))}
        </ul>
        {sheet.diagnosticNote !== null ? (
          <p className="notice">{sheet.diagnosticNote}</p>
        ) : null}
      </div>

      <div className="plugin__section">
        <h3 className="plugin__section-title">Tools</h3>
        {sheet.toolsNotice !== null ? (
          <p className="notice">{sheet.toolsNotice}</p>
        ) : (
          <ul className="plugin__tool-list">
            {sheet.tools.map((tool) => (
              <li className="plugin__tool mono" key={tool}>
                {tool}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="plugin__section">
        <h3 className="plugin__section-title">Instruction</h3>
        {sheet.instructionNote !== null ? (
          <p className="notice">{sheet.instructionNote}</p>
        ) : (
          <p className="plugin__instruction">{sheet.instruction}</p>
        )}
      </div>
    </div>
  );
}

/** Une ligne du registre : rail d'état, nom, état, Tools, fiche dépliable. */
function RegistryRow({ plugin }: { plugin: PluginInfo }) {
  const row = pluginRow(plugin);
  return (
    <li className="registry__row">
      <details className="registry__details">
        <summary className="registry__summary">
          <span
            className={`registry__rail registry__rail--${row.motif}`}
            aria-hidden="true"
          />
          <span className="registry__entry">
            <span className="registry__name mono">{row.nameLabel}</span>
            <span className="registry__description">{row.descriptionLabel}</span>
          </span>
          <span className="registry__state">
            <span
              className={`registry__state-label registry__state-label--${row.stateClass}`}
            >
              {row.stateLabel}
            </span>
            <span className="registry__tools-count">{row.toolsLabel}</span>
          </span>
        </summary>
        <PluginSheetView plugin={plugin} />
      </details>
    </li>
  );
}

/**
 * Section plein largeur : registre technique des plugins installés.
 *
 * Compteurs, recherche et filtre se recalculent à chaque instantané reçu : une
 * trame `status` plus récente remplace la précédente, donc un état affiché
 * n'est jamais figé. Aucun contrôle n'écrit ni n'active : consulter ne change
 * rien côté serveur.
 */
export function PluginRegistry({
  plugins,
}: {
  plugins: readonly PluginInfo[];
}) {
  const [filter, setFilter] = useState<PluginFilter>(EMPTY_PLUGIN_FILTER);
  const counters = pluginCounters(plugins);
  const items = pluginCounterItems(counters);
  const visible = filterPlugins(plugins, filter);
  const result = pluginResultSummary(filter, {
    total: plugins.length,
    shown: visible.length,
  });

  return (
    <section className="registry" aria-labelledby="registry-title">
      <header className="registry__header">
        <div>
          <h2 className="registry__title" id="registry-title">
            Plugin Registry
          </h2>
          <p className="registry__subtitle">
            Plugins installés · lecture seule
          </p>
        </div>
        <dl className="registry__counters">
          {items.map((item) => (
            <div className="registry__counter" key={item.key}>
              <dt>{item.label}</dt>
              <dd className="registry__counter-value">{item.value}</dd>
            </div>
          ))}
        </dl>
      </header>

      <div className="registry__controls">
        <label className="field field--search" htmlFor="registry-search">
          <span className="field__label">Recherche</span>
          <input
            className="field__input"
            id="registry-search"
            type="search"
            value={filter.text}
            placeholder="nom, description ou Tool…"
            onChange={(event) =>
              setFilter((current) => ({
                ...current,
                text: event.target.value,
              }))
            }
            spellCheck={false}
          />
        </label>

        <label className="field field--filter" htmlFor="registry-state">
          <span className="field__label">État de service</span>
          <select
            className="field__input mono"
            id="registry-state"
            value={filter.state}
            onChange={(event) =>
              setFilter((current) => ({
                ...current,
                state: event.target.value,
              }))
            }
          >
            {PLUGIN_STATE_FILTERS.map((option) => (
              <option key={option.value || "all"} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <p
        className={`registry__result${
          result.empty ? " registry__result--empty" : ""
        }`}
        role="status"
        aria-live="polite"
      >
        {result.label}
      </p>

      {visible.length > 0 ? (
        <ul className="registry__list">
          {visible.map((plugin, index) => (
            <RegistryRow
              key={plugin.name || `plugin-${index}`}
              plugin={plugin}
            />
          ))}
        </ul>
      ) : null}
    </section>
  );
}
