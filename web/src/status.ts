/**
 * Instantané d'état de la Console : types et normalisation générale.
 *
 * Le contrat JSON reste en `snake_case` côté serveur. Ces fonctions pures
 * normalisent un instantané reçu par WebSocket (dont les champs sont inconnus
 * à la compilation) et délèguent la fiche des Plugins à `plugins.ts`, qui porte
 * aussi les libellés français de leurs États de service.
 */

import { normalizePlugins, type PluginInfo } from "./plugins";
import type { ClientInfo, SessionStatus } from "./session";

/** Plugin tel que l'instantané le décrit (voir `plugins.ts`). */
export type { PluginInfo } from "./plugins";

/** Configuration non sensible exposée par l'instantané. */
export type ConsoleConfig = Record<string, string>;

/** État complet de la Console, sans l'enveloppe versionnée. */
export interface StatusData {
  session: SessionStatus;
  clients: ClientInfo[];
  plugins: PluginInfo[];
  config: ConsoleConfig;
}

/** Forme minimale d'une trame `hello`, sans coupler ce module aux traces. */
export interface HelloLike {
  data: { snapshot: Record<string, unknown> };
}

/** État vide : ce que la page affiche avant le premier instantané. */
export function emptyStatus(): StatusData {
  return {
    session: {
      state: "",
      session_id: "",
      connection_id: "",
      active_client_id: "",
    },
    clients: [],
    plugins: [],
    config: {},
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** Normalise un instantané reçu par WebSocket, sans jamais lever d'exception. */
export function normalizeStatus(raw: unknown): StatusData {
  const root = isRecord(raw) ? raw : {};
  const session = isRecord(root.session) ? root.session : {};
  const config = isRecord(root.config) ? root.config : {};

  return {
    session: {
      state: asText(session.state),
      session_id: asText(session.session_id),
      connection_id: asText(session.connection_id),
      active_client_id: asText(session.active_client_id),
    },
    clients: asList(root.clients)
      .filter(isRecord)
      .map((client) => ({
        client_id: asText(client.client_id),
        client_name: asText(client.client_name),
        platform: asText(client.platform),
      })),
    plugins: normalizePlugins(root.plugins),
    config: Object.fromEntries(
      Object.entries(config).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string",
      ),
    ),
  };
}

/**
 * Instantané porté par une trame `hello`.
 *
 * Une reconnexion remplace l'état affiché : le `hello` fait foi, on ne fusionne
 * jamais avec l'état d'une connexion précédente.
 */
export function statusFromHello(frame: HelloLike): StatusData {
  return normalizeStatus(frame.data?.snapshot);
}
