/**
 * Tour texte : soumission et flux SSE (ADR-0024).
 *
 * La requête part en même origine ; le cookie du Ticket part seul. Un refus
 * HTTP devient une `ApiError`, mais une annulation volontaire traverse telle
 * quelle : c'est elle qui referme la réponse et annule le Tour côté serveur.
 */

import { ApiError } from "./errors";
import { errorFromResponse, type Fetcher } from "./client";

/** Même plafond que le serveur : un message trop long est refusé avant l'envoi. */
export const MAX_MESSAGE_CHARS = 4096;

/**
 * Ouvre un Tour et rend la réponse en flux.
 *
 * L'appelant lit le corps, ferme le lecteur ou annule le `AbortSignal` : dans
 * tous ces cas le serveur annule le Tour, jamais ne le laisse orphelin.
 */
export async function openTour(
  text: string,
  signal: AbortSignal,
  fetcher: Fetcher = fetch,
): Promise<Response> {
  let response: Response;
  try {
    response = await fetcher("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text }),
      signal,
    });
  } catch (reason) {
    if (signal.aborted || isAbort(reason)) {
      throw reason;
    }
    throw new ApiError(0, "network");
  }

  if (!response.ok) {
    throw await errorFromResponse(response);
  }
  return response;
}

function isAbort(reason: unknown): boolean {
  return (
    typeof reason === "object" &&
    reason !== null &&
    "name" in reason &&
    (reason as { name?: unknown }).name === "AbortError"
  );
}
