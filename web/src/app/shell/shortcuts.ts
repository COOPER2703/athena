/** Raccourcis clavier globaux de la coquille, sans DOM. */

import type { ZoneId } from "../routing";

/** Raccourci d'ouverture de la palette : Cmd+K sur macOS, Ctrl+K ailleurs. */
export function isPaletteShortcut(key: {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
}): boolean {
  return (
    key.key.toLowerCase() === "k" &&
    (key.metaKey === true || key.ctrlKey === true)
  );
}

/** État de la séquence `g` : la touche attend la suivante. */
export interface ShortcutState {
  chord: boolean;
}

export const IDLE_SHORTCUT_STATE: ShortcutState = { chord: false };

const ZONE_KEYS: Readonly<Record<string, ZoneId>> = {
  c: "conversation",
  s: "settings",
  p: "plugins",
  d: "diagnostic",
};

/**
 * Avance la machine des raccourcis d'une touche.
 *
 * `g` arme la séquence ; la lettre suivante choisit une zone. Toute autre
 * touche après `g` abandonne la séquence, sans effet de bord.
 */
export function reduceShortcut(
  state: ShortcutState,
  key: string,
): { state: ShortcutState; zone?: ZoneId } {
  const normalized = key.toLowerCase();
  if (state.chord) {
    const zone = ZONE_KEYS[normalized];
    return { state: IDLE_SHORTCUT_STATE, zone };
  }
  if (normalized === "g") {
    return { state: { chord: true } };
  }
  return { state: IDLE_SHORTCUT_STATE };
}

/** Cible clavier minimale, pour ne pas détourner la frappe d'un champ. */
export interface KeyboardTarget {
  tagName: string;
  isContentEditable: boolean;
}

/** Vrai quand la touche doit rester au champ qui a le focus. */
export function isEditableTarget(
  target: KeyboardTarget | null | undefined,
): boolean {
  if (!target) {
    return false;
  }
  if (target.isContentEditable) {
    return true;
  }
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}
