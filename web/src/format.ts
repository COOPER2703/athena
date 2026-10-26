/**
 * Formatage d'une Trace pour l'affichage, sans navigateur.
 *
 * Ces fonctions pures ne connaissent ni le flux ni la connexion : elles
 * traduisent une Trace déjà reçue en texte lisible.
 */

/** Horodatage en heure locale, avec millisecondes pour les logs denses. */
export function formatTimestamp(seconds: number): string {
  const date = new Date(seconds * 1000);
  const pad = (value: number, width = 2) => String(value).padStart(width, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}.${pad(date.getMilliseconds(), 3)}`;
}

/** Payload lisible : JSON indenté, ou chaîne vide s'il n'y a rien à montrer. */
export function formatPayload(payload: Record<string, unknown>): string {
  if (Object.keys(payload).length === 0) {
    return "";
  }
  return JSON.stringify(payload, null, 2);
}
