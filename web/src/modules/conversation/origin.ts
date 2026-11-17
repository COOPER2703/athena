/** Origine d'un tour du fil : libellé et tonalité, jamais la couleur seule. */

import type { ThreadOrigin } from "../../api/thread";
import type { BadgeTone } from "../../shared/ui/Badge";

const LABELS: Readonly<Record<ThreadOrigin, string>> = {
  text: "Clavier",
  voice: "Voix",
  event: "Annonce",
  unknown: "Origine inconnue",
};

const TONES: Readonly<Record<ThreadOrigin, BadgeTone>> = {
  text: "neutral",
  voice: "accent",
  event: "warning",
  unknown: "neutral",
};

/** Libellé français de l'origine d'un tour. */
export function originLabel(origin: ThreadOrigin): string {
  return LABELS[origin];
}

/** Tonalité du badge d'origine ; le libellé porte le sens, pas la couleur. */
export function originTone(origin: ThreadOrigin): BadgeTone {
  return TONES[origin];
}
