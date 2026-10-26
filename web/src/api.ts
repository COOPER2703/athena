/** Accès à l'API JSON de la Console. */

import type { ClientInfo, SessionStatus } from "./session";

/** Plugin tel que l'instantané le décrit (non affiché en v1). */
export interface PluginInfo {
  name: string;
  state: string;
  tools: string[];
}

/** Réponse de `GET /api/status`, enveloppe versionnée comprise. */
export interface StatusPayload {
  v: number;
  session: SessionStatus;
  clients: ClientInfo[];
  plugins: PluginInfo[];
  config: Record<string, string>;
}

/** Lit l'instantané courant ; une réponse non-2xx devient une erreur lisible. */
export async function fetchStatus(signal?: AbortSignal): Promise<StatusPayload> {
  const response = await fetch("/api/status", {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`Le serveur a répondu ${response.status}.`);
  }
  return (await response.json()) as StatusPayload;
}
