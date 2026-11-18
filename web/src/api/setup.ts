/** Création de l'unique Compte : l'Installation, sans Code d'installation. */

import { sessionFromSetup, type SetupOutcome } from "../auth/session";
import {
  jsonBody,
  requestJson,
  type Credentials,
  type Fetcher,
} from "./client";

export const SETUP_PATH = "/api/setup";

/**
 * Crée l'unique Compte et ouvre un Ticket Console.
 *
 * La réponse porte la clé maître **une seule fois** ; elle n'est rendue qu'à
 * l'appelant, qui la garde en mémoire le temps de l'afficher.
 */
export async function setupAccount(
  credentials: Credentials,
  fetcher?: Fetcher,
): Promise<SetupOutcome> {
  const body = await requestJson(
    SETUP_PATH,
    { method: "POST", ...jsonBody(credentials) },
    fetcher,
  );
  return sessionFromSetup(body);
}
