/** Palette de commandes : recherche et sélection, sans DOM. */

/** Action proposée par la palette. */
export interface Command {
  id: string;
  label: string;
  keywords?: readonly string[];
  /** Raccourci affiché à droite, par exemple `g p`. */
  shortcut?: string;
  /** Groupe affiché au-dessus de la commande. */
  group: string;
  run: () => void;
}

/** Forme comparable d'une recherche : minuscules, sans accents, sans espaces. */
export function normalizeSearch(value: string): string {
  return value
    .toLowerCase()
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Filtre les commandes : chaque mot de la recherche doit apparaître dans le
 * libellé ou les mots-clés, dans n'importe quel ordre.
 */
export function filterCommands(
  query: string,
  commands: readonly Command[],
): Command[] {
  const terms = normalizeSearch(query).split(" ").filter(Boolean);
  if (terms.length === 0) {
    return [...commands];
  }
  return commands.filter((command) => {
    const haystack = normalizeSearch(
      [command.label, ...(command.keywords ?? [])].join(" "),
    );
    return terms.every((term) => haystack.includes(term));
  });
}

/** Index voisin dans une liste circulaire ; `-1` si la liste est vide. */
export function moveSelection(
  current: number,
  delta: number,
  length: number,
): number {
  if (length <= 0) {
    return -1;
  }
  const start = current >= 0 && current < length ? current : 0;
  return (start + delta + length) % length;
}
