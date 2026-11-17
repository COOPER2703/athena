/**
 * États de chargement d'un Plugin : une seule table pour le Diagnostic et la
 * zone Plugins.
 *
 * Module **pur** : aucun DOM, aucun `fetch`. Un état inconnu — une version plus
 * récente du serveur — garde son nom brut et une tonalité neutre, jamais une
 * couleur qui prétendrait le connaître.
 */

import type { BadgeTone } from "./ui/Badge";

/** États connus du Plugin Registry (GLOSSARY, section Plugins). */
export type PluginState = "activated" | "disabled" | "missing" | "failed";

const PLUGIN_STATES: Readonly<Record<PluginState, string>> = {
  activated: "Activé",
  disabled: "Désactivé",
  missing: "Manquant",
  failed: "En échec",
};

/** Vrai si l'état est un état connu du Plugin Registry. */
export function isPluginState(state: string): state is PluginState {
  return Object.prototype.hasOwnProperty.call(PLUGIN_STATES, state);
}

/** Libellé d'un état de Plugin ; l'état brut passe s'il est inconnu. */
export function formatPluginState(state: string): string {
  if (!state) {
    return "Inconnu";
  }
  return isPluginState(state) ? PLUGIN_STATES[state] : state;
}

/** Tonalité d'un état de Plugin ; aucun état inconnu ne choisit une couleur. */
export function pluginStateTone(state: string): BadgeTone {
  switch (state) {
    case "activated":
      return "accent";
    case "failed":
      return "danger";
    case "missing":
      return "warning";
    default:
      return "neutral";
  }
}
