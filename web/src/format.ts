/**
 * Formatage d'une Trace pour l'affichage, sans navigateur.
 *
 * Ces fonctions pures ne connaissent ni le flux ni la connexion : elles
 * traduisent une Trace déjà reçue en texte lisible.
 */

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

/** Horodatage en heure locale, avec millisecondes pour les logs denses. */
export function formatTimestamp(seconds: number): string {
  const date = new Date(seconds * 1000);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}.${pad(date.getMilliseconds(), 3)}`;
}

/** Date courte en heure locale (`jj/mm/aaaa hh:mm`), ou un tiret si absente. */
export function formatTokenDate(secondsValue: number | null): string {
  if (secondsValue === null || !Number.isFinite(secondsValue)) {
    return "—";
  }
  const date = new Date(secondsValue * 1000);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return `${pad(date.getDate())}/${pad(date.getMonth() + 1)}/${date.getFullYear()} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** Payload lisible : JSON indenté, ou chaîne vide s'il n'y a rien à montrer. */
export function formatPayload(payload: Record<string, unknown>): string {
  if (Object.keys(payload).length === 0) {
    return "";
  }
  return JSON.stringify(payload, null, 2);
}
