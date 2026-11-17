import { describe, expect, it } from "vitest";

import {
  filterCommands,
  moveSelection,
  normalizeSearch,
  type Command,
} from "./palette";

function command(id: string, label: string, keywords: string[] = []): Command {
  return {
    id,
    label,
    keywords,
    group: "Navigation",
    run: () => undefined,
  };
}

const COMMANDS: readonly Command[] = [
  command("go-conversation", "Aller à la Conversation", ["fil", "chat"]),
  command("go-settings", "Aller aux Réglages", ["voix", "mémoire"]),
  command("theme", "Basculer le thème", ["sombre", "clair"]),
  command("logout", "Se déconnecter", ["quitter"]),
];

describe("normalizeSearch", () => {
  it("ignore la casse, les accents et les espaces superflus", () => {
    expect(normalizeSearch("  Réglages ")).toBe("reglages");
    expect(normalizeSearch("DÉCONNEXION")).toBe("deconnexion");
  });
});

describe("filterCommands", () => {
  it("rend toutes les commandes sans recherche", () => {
    expect(filterCommands("", COMMANDS)).toHaveLength(4);
    expect(filterCommands("   ", COMMANDS)).toHaveLength(4);
  });

  it("cherche dans le libellé et les mots-clés", () => {
    expect(filterCommands("reglage", COMMANDS).map((item) => item.id)).toEqual([
      "go-settings",
    ]);
    expect(filterCommands("voix", COMMANDS).map((item) => item.id)).toEqual([
      "go-settings",
    ]);
    expect(filterCommands("theme", COMMANDS).map((item) => item.id)).toEqual([
      "theme",
    ]);
  });

  it("exige tous les mots, dans n'importe quel ordre", () => {
    expect(
      filterCommands("aller reglages", COMMANDS).map((item) => item.id),
    ).toEqual(["go-settings"]);
    expect(filterCommands("aller theme", COMMANDS)).toHaveLength(0);
  });

  it("rend une liste vide quand rien ne correspond", () => {
    expect(filterCommands("zzz", COMMANDS)).toEqual([]);
  });
});

describe("moveSelection", () => {
  it("descend et remonte en bouclant", () => {
    expect(moveSelection(0, 1, 4)).toBe(1);
    expect(moveSelection(3, 1, 4)).toBe(0);
    expect(moveSelection(0, -1, 4)).toBe(3);
  });

  it("reste sûr sur une liste vide", () => {
    expect(moveSelection(0, 1, 0)).toBe(-1);
    expect(moveSelection(2, 1, 0)).toBe(-1);
  });
});
