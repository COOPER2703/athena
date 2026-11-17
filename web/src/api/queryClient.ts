/**
 * Client TanStack Query de la Console : une seule politique pour toutes les
 * requêtes, et un seul endroit qui réagit à l'expiration du Ticket Console.
 */

import { QueryCache, QueryClient } from "@tanstack/react-query";

import { isApiError, isUnauthorized } from "./errors";

/** Deux réessais suffisent : au-delà, la panne est durable. */
const MAX_RETRIES = 2;

/**
 * Réessaie les pannes de transport et les 5xx ; jamais un refus métier, qu'il
 * soit d'authentification, de validation ou de quota.
 *
 * La signature suit celle de TanStack Query : `(failureCount, error)`.
 */
export function isRetryable(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_RETRIES) {
    return false;
  }
  return isApiError(error) && (error.status === 0 || error.status >= 500);
}

type UnauthorizedListener = () => void;

const unauthorizedListeners = new Set<UnauthorizedListener>();

/**
 * Abonnement à l'expiration du Ticket.
 *
 * `requestJson` ne connaît pas le routeur : c'est ce signal, émis par le cache
 * de requêtes, que la coquille écoute pour ramener à l'écran de connexion.
 */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

function notifyUnauthorized(): void {
  for (const listener of unauthorizedListeners) {
    listener();
  }
}

/** Client partagé : requêtes mises en cache, erreurs 401 centralisées. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        // Seules les requêtes comptent : un 401 de mutation (`/api/login`)
        // signifie « identifiants faux » et reste affiché dans le formulaire.
        if (isUnauthorized(error)) {
          notifyUnauthorized();
        }
      },
    }),
    defaultOptions: {
      queries: {
        retry: isRetryable,
        staleTime: 15_000,
        refetchOnWindowFocus: false,
      },
    },
  });
}

/** Clés de requête, une seule source pour invalider ou lire le cache. */
export const queryKeys = {
  session: ["session"] as const,
  status: ["status"] as const,
  plugins: ["plugins"] as const,
} as const;
