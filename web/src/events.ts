/**
 * Injection d'un Événement depuis la Console, sans navigateur.
 *
 * La validation reprend exactement les règles du serveur (module partagé
 * `event_body.py`) : champs non vides après trim, plafond de 4 096 caractères.
 * Elle est faite **avant** l'envoi, pour qu'une saisie vide ou trop longue ne
 * produise aucune requête réseau. Les fonctions sont pures ou reçoivent leur
 * `fetch`, donc testables par `vitest` sans DOM.
 */

/** Plafond de contenu, miroir de `MAX_CONTENT_CHARS` côté serveur. */
export const MAX_CONTENT_CHARS = 4096;
/** Source par défaut d'un Événement injecté depuis la Console. */
export const DEFAULT_SOURCE = "console";
/** Accusé rendu quand l'API accepte l'Événement. */
export const ACCEPTED_MESSAGE = "Événement accepté";

/** Brouillon saisi dans le formulaire. */
export interface EventDraft {
  source: string;
  content: string;
}

/** Saisie validée, prête à être envoyée. */
export interface ValidEvent {
  ok: true;
  source: string;
  content: string;
}

/** Saisie refusée avant tout envoi, avec un message en français. */
export interface InvalidEvent {
  ok: false;
  message: string;
}

export type EventValidation = ValidEvent | InvalidEvent;

/** Résultat d'une tentative d'injection, prêt à afficher. */
export type SubmissionResult =
  | { ok: true; message: string }
  | { ok: false; message: string; unauthorized?: boolean };

/** Nombre de caractères utiles : les espaces de bord ne comptent pas. */
export function characterCount(content: string): number {
  return content.trim().length;
}

/** Valide une saisie et rend la version nettoyée, ou un refus lisible. */
export function validateEventDraft(draft: EventDraft): EventValidation {
  const source = draft.source.trim();
  const content = draft.content.trim();
  if (!source) {
    return { ok: false, message: "La Source ne peut pas être vide." };
  }
  if (!content) {
    return {
      ok: false,
      message: "Le contenu de l'Événement ne peut pas être vide.",
    };
  }
  if (content.length > MAX_CONTENT_CHARS) {
    return {
      ok: false,
      message: `Le contenu dépasse 4 096 caractères (${content.length}).`,
    };
  }
  return { ok: true, source, content };
}

/** Message d'erreur lisible : celui de l'API d'abord, le statut sinon. */
export function eventErrorMessage(status: number, body: unknown): string {
  if (typeof body === "object" && body !== null && "error" in body) {
    const error = (body as { error?: unknown }).error;
    if (typeof error === "string" && error.trim()) {
      return error;
    }
  }
  return `Le serveur a refusé l'Événement (${status}).`;
}

/** Signature minimale de `fetch`, injectable dans les tests. */
export type Fetcher = (
  input: string,
  init: RequestInit,
) => Promise<Response>;

/**
 * Valide puis envoie l'Événement à `POST /api/events`.
 *
 * Une saisie refusée ne déclenche aucune requête : le refus est rendu
 * localement. Une réponse non 202 devient une erreur affichable, en préférant
 * le message de l'API.
 */
export async function submitDraft(
  draft: EventDraft,
  fetcher: Fetcher = fetch,
): Promise<SubmissionResult> {
  const validation = validateEventDraft(draft);
  if (!validation.ok) {
    return { ok: false, message: validation.message };
  }

  let response: Response;
  try {
    response = await fetcher("/api/events", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        source: validation.source,
        content: validation.content,
      }),
    });
  } catch {
    return { ok: false, message: "Impossible de joindre l'API de la Console." };
  }

  if (response.status === 202) {
    return { ok: true, message: ACCEPTED_MESSAGE };
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return {
    ok: false,
    message: eventErrorMessage(response.status, body),
    ...(response.status === 401 ? { unauthorized: true } : {}),
  };
}
