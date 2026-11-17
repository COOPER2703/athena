/**
 * Secrets de la Console : écriture seule.
 *
 * Aucune valeur n'est jamais relue : `GET /api/secrets` ne rend que des noms et
 * des dates. Ce module n'expose donc que l'écriture et l'effacement.
 */

import { jsonBody, requestJson, type Fetcher } from "./client";

/** Nom du Secret Gemini : c'est aussi le nom de la variable attendue. */
export const GEMINI_SECRET = "GEMINI_API_KEY";

/** Enregistre un Secret ; le serveur le valide avant de le chiffrer. */
export async function saveSecret(
  name: string,
  value: string,
  fetcher?: Fetcher,
): Promise<void> {
  await requestJson(
    `/api/secrets/${encodeURIComponent(name)}`,
    { method: "PUT", ...jsonBody({ value }) },
    fetcher,
  );
}
