/** Thème de la Console : sombre par défaut, clair dérivé des mêmes tokens. */

export type Theme = "dark" | "light";

export const DEFAULT_THEME: Theme = "dark";

/** Clé de persistance : un choix par navigateur, jamais côté serveur. */
export const THEME_STORAGE_KEY = "athena.theme";

/** Valide une valeur stockée ; tout le reste retombe sur le défaut. */
export function parseTheme(value: unknown): Theme | null {
  return value === "dark" || value === "light" ? value : null;
}

/** Thème initial : le choix explicite, sinon le sombre. */
export function initialTheme(stored: string | null): Theme {
  return parseTheme(stored) ?? DEFAULT_THEME;
}

/** Autre thème, pour un bouton qui bascule. */
export function nextTheme(theme: Theme): Theme {
  return theme === "dark" ? "light" : "dark";
}

/** Libellé français du thème. */
export function themeLabel(theme: Theme): string {
  return theme === "dark" ? "Sombre" : "Clair";
}

/**
 * Applique le thème à la racine du document.
 *
 * `data-theme` sélectionne le jeu de tokens ; `color-scheme` aligne les
 * contrôles natifs (barres de défilement, champs) sur le même thème.
 */
export function applyTheme(theme: Theme, root: HTMLElement): void {
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
}
