/**
 * Brouillons de saisie conservés entre deux écrans.
 *
 * Un Ticket Console peut expirer pendant qu'on écrit : la saisie survit dans
 * le stockage de l'onglet, puis l'écran qui la reprend la relit. Aucun contenu
 * ne part au serveur et rien n'est partagé entre onglets.
 */

export interface DraftStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const PREFIX = "athena.draft.";

/** Relit un brouillon ; un stockage indisponible rend la chaîne vide. */
export function readDraft(storage: DraftStorage, key: string): string {
  try {
    return storage.getItem(PREFIX + key) ?? "";
  } catch {
    return "";
  }
}

/** Écrit un brouillon ; une valeur vide l'efface. */
export function writeDraft(
  storage: DraftStorage,
  key: string,
  value: string,
): void {
  try {
    if (value === "") {
      storage.removeItem(PREFIX + key);
      return;
    }
    storage.setItem(PREFIX + key, value);
  } catch {
    // Quota plein ou stockage refusé : le brouillon est un confort, pas une
    // garantie, et la saisie en cours reste dans le composant.
  }
}

/** Efface un brouillon, par exemple après un envoi réussi. */
export function clearDraft(storage: DraftStorage, key: string): void {
  try {
    storage.removeItem(PREFIX + key);
  } catch {
    // Même raison : l'absence de nettoyage ne casse pas la saisie.
  }
}
