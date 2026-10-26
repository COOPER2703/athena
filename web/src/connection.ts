/**
 * Connexion WebSocket de la Console : états affichés et backoff de reconnexion.
 *
 * Fonctions pures et déterministes, séparées du flux de traces pour que la
 * machine de reconnexion se teste sans navigateur.
 */

/** État de la connexion WebSocket, tel que l'en-tête l'affiche. */
export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";

/** Libellé français de l'état de connexion. */
export function connectionLabel(status: ConnectionStatus): string {
  switch (status) {
    case "connected":
      return "Connecté";
    case "connecting":
      return "Connexion…";
    case "reconnecting":
      return "Reconnexion…";
    case "disconnected":
      return "Déconnecté";
  }
}

/** Options du backoff exponentiel de reconnexion. */
export interface BackoffOptions {
  baseMs: number;
  maxMs: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = { baseMs: 500, maxMs: 30_000 };

/**
 * Délai avant la tentative suivante : double à chaque échec, plafonné.
 *
 * Fonction pure et déterministe (pas de jitter) : c'est la machine de
 * reconnexion qu'on teste, pas le hasard.
 */
export function backoffDelay(
  attempt: number,
  options: BackoffOptions = DEFAULT_BACKOFF,
): number {
  const step = Math.max(0, Math.floor(attempt));
  return Math.min(options.baseMs * 2 ** step, options.maxMs);
}
