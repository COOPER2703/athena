/** Accès à l'API JSON de la Console. */

import { normalizeStatus, type StatusData } from "./status";

/** Réponse de `GET /api/status`, enveloppe versionnée comprise. */
export interface StatusPayload extends StatusData {
  v: number;
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
  const body: unknown = await response.json();
  const version =
    typeof body === "object" && body !== null && "v" in body
      ? Number((body as { v?: unknown }).v)
      : NaN;
  return {
    v: Number.isFinite(version) ? version : 0,
    ...normalizeStatus(body),
  };
}
