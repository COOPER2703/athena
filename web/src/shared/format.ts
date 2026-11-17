/**
 * Mise en forme des données affichées par la Console.
 *
 * Les données (horodatages, durées, tailles, identifiants) sont rendues en
 * monospace à l'écran ; ces fonctions pures choisissent seulement l'unité et la
 * langue, jamais la mise en page.
 */

const pad = (value: number): string => String(value).padStart(2, "0");

function toDate(seconds: number): Date | null {
  if (!Number.isFinite(seconds)) {
    return null;
  }
  const date = new Date(seconds * 1000);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Heure locale avec secondes (`hh:mm:ss`) ; le fuseau est injectable en test. */
export function formatTimestamp(seconds: number, timeZone?: string): string {
  const date = toDate(seconds);
  if (date === null) {
    return "—";
  }
  return new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone,
  }).format(date);
}

/** Date courte (`jj/mm/aaaa hh:mm`), ou un tiret si elle manque. */
export function formatDate(
  seconds: number | null,
  timeZone?: string,
): string {
  if (seconds === null) {
    return "—";
  }
  const date = toDate(seconds);
  if (date === null) {
    return "—";
  }
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone,
  }).format(date);
}

/** Durée lisible : secondes, minutes, heures, jours — la plus grande unité. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) {
    return "—";
  }
  const total = Math.floor(seconds);
  if (total < 60) {
    return `${total} s`;
  }
  if (total < 3600) {
    return `${Math.floor(total / 60)} min`;
  }
  if (total < 86_400) {
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return `${hours} h ${pad(minutes)} min`;
  }
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3600);
  return `${days} j ${hours} h`;
}

const BYTE_UNITS = ["o", "Ko", "Mo", "Go", "To"] as const;

/** Taille en unités françaises, une décimale au plus. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "—";
  }
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rendered = new Intl.NumberFormat("fr-FR", {
    maximumFractionDigits: 1,
  }).format(value);
  return `${rendered} ${BYTE_UNITS[unit]}`;
}

/** Identifiant raccourci pour rester lisible dans une ligne dense. */
export function shortId(value: string, maxLength = 8): string {
  if (!value) {
    return "—";
  }
  return value.length <= maxLength ? value : `${value.slice(0, maxLength)}…`;
}

/** Nombre suivi d'un nom accordé ; zéro reste au singulier en français. */
export function pluralize(
  count: number,
  singular: string,
  plural = `${singular}s`,
): string {
  return `${count} ${count > 1 ? plural : singular}`;
}
