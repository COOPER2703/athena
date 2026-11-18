/**
 * Préférences d'affichage de la Console, conservées sur le poste.
 *
 * Elles ne portent jamais de secret : seulement des choix d'interface qu'un
 * rechargement doit retrouver. Un stockage indisponible (mode privé, quota
 * plein) ne casse rien : la valeur par défaut prend le relais.
 */

/** Ce dont ces fonctions ont besoin d'un stockage, sans dépendre du DOM. */
export interface PreferenceStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** Clé de la sourdine de la Conversation. */
export const MUTED_PREFERENCE_KEY = "athena.conversation.muted";

/**
 * Vrai quand le son doit rester coupé.
 *
 * La sourdine est le défaut (ADR-0024) : une valeur absente ou illisible coupe
 * le son, jamais l'inverse.
 */
export function readMuted(storage: PreferenceStorage | undefined): boolean {
  if (storage === undefined) {
    return true;
  }
  try {
    return storage.getItem(MUTED_PREFERENCE_KEY) !== "0";
  } catch {
    return true;
  }
}

/** Enregistre le choix de sourdine ; un stockage qui refuse est ignoré. */
export function writeMuted(
  storage: PreferenceStorage | undefined,
  muted: boolean,
): void {
  if (storage === undefined) {
    return;
  }
  try {
    storage.setItem(MUTED_PREFERENCE_KEY, muted ? "1" : "0");
  } catch {
    // Le choix vaut pour cette visite ; il ne survivra pas au rechargement.
  }
}
