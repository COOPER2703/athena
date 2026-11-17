/** Copie dans le presse-papiers, avec un échec explicite plutôt que silencieux. */

/**
 * Copie un texte et rend `true` en cas de succès.
 *
 * `navigator.clipboard` manque hors contexte sécurisé : l'appelant affiche
 * alors une consigne de sélection manuelle. Un Jeton n'est jamais journalisé.
 */
export async function copyText(value: string): Promise<boolean> {
  if (typeof navigator === "undefined" || navigator.clipboard === undefined) {
    return false;
  }
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}
