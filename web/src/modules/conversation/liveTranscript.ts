/**
 * Fil en direct : lignes vivantes de la conversation vocale et Journal.
 *
 * Module **pur** : il ne lit ni le réseau ni l'état du noyau. Le Journal est la
 * vérité persistée (`GET /api/thread`) ; les transcriptions reçues pendant la
 * conversation sont des lignes **transitoires** qui précèdent leur entrée.
 *
 * Règle de déduplication, volontairement stricte :
 *
 * - seules les entrées d'origine `voice` **après le repère de départ** de la
 *   conversation peuvent reprendre une ligne vivante — un Tour clavier, une
 *   Annonce ou une parole d'avant la conversation ne l'absorbent jamais ;
 * - la correspondance porte sur le couple `role` + `text`, **à hauteur du
 *   nombre d'entrées** : chaque entrée ne reprend qu'une ligne. Deux « oui »
 *   successifs restent donc visibles à tour de rôle, et un énoncé légitime
 *   répété n'est pas effacé par une entrée plus ancienne.
 *
 * L'ordre de rendu est celui du Journal, puis les lignes encore vivantes : ce
 * sont toujours les plus récentes, puisque chaque fin de tour relit le Journal
 * et que les lignes reprises disparaissent de l'affichage.
 */

import type { ThreadEntry } from "../../api/thread";
import type { VoiceLine } from "./voice";

/** Une ligne du fil, prête à afficher : entrée du Journal ou parole vivante. */
export interface LiveTranscriptLine {
  /** Clé de rendu stable : l'entrée pour le Journal, son rang pour le direct. */
  key: string;
  role: string;
  origin: ThreadEntry["origin"];
  text: string;
  /** Entrée du Journal reprise, ou `null` pour une ligne encore vivante. */
  entry: ThreadEntry | null;
}

/**
 * Fusionne le Journal et les transcriptions encore vivantes.
 *
 * `journalBaseline` est l'identifiant maximal du Journal au démarrage de la
 * conversation : les entrées antérieures décrivent un autre moment et ne
 * peuvent pas reprendre les mots prononcés maintenant.
 */
export function mergeLiveTranscript(
  entries: readonly ThreadEntry[],
  transient: readonly VoiceLine[],
  journalBaseline: number,
): LiveTranscriptLine[] {
  const available = confirmableCounts(entries, journalBaseline);
  const lines: LiveTranscriptLine[] = entries.map((entry) => ({
    key: `journal-${entry.id}`,
    role: entry.role,
    origin: entry.origin,
    text: entry.text,
    entry,
  }));

  transient.forEach((line, index) => {
    const key = pairKey(line.role, line.text);
    const remaining = available.get(key) ?? 0;
    if (remaining > 0) {
      available.set(key, remaining - 1);
      return;
    }
    lines.push({
      key: `live-${index}`,
      role: line.role,
      origin: "voice",
      text: line.text,
      entry: null,
    });
  });

  return lines;
}

/** Combien de lignes chaque couple `role` + `text` peut reprendre. */
function confirmableCounts(
  entries: readonly ThreadEntry[],
  journalBaseline: number,
): Map<string, number> {
  const counts = new Map<string, number>();
  for (const entry of entries) {
    if (entry.origin !== "voice" || entry.id <= journalBaseline) {
      continue;
    }
    const key = pairKey(entry.role, entry.text);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

function pairKey(role: string, text: string): string {
  return `${role}\u0000${text}`;
}
