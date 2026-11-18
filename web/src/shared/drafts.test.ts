import { describe, expect, it } from "vitest";

import { clearDraft, readDraft, writeDraft, type DraftStorage } from "./drafts";

function memoryStorage(): DraftStorage & { values: Map<string, string> } {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => {
      values.set(key, value);
    },
    removeItem: (key) => {
      values.delete(key);
    },
  };
}

describe("brouillons", () => {
  it("relit la saisie laissée avant une reconnexion", () => {
    const storage = memoryStorage();
    writeDraft(storage, "conversation.message", "Ma question en cours");
    expect(readDraft(storage, "conversation.message")).toBe(
      "Ma question en cours",
    );
  });

  it("rend une chaîne vide quand il n'y a rien", () => {
    expect(readDraft(memoryStorage(), "absent")).toBe("");
  });

  it("efface un brouillon envoyé", () => {
    const storage = memoryStorage();
    writeDraft(storage, "conversation.message", "texte");
    clearDraft(storage, "conversation.message");
    expect(readDraft(storage, "conversation.message")).toBe("");
  });

  it("survit à un stockage indisponible", () => {
    const broken: DraftStorage = {
      getItem: () => {
        throw new Error("refusé");
      },
      setItem: () => {
        throw new Error("plein");
      },
      removeItem: () => {
        throw new Error("refusé");
      },
    };
    expect(readDraft(broken, "x")).toBe("");
    expect(() => writeDraft(broken, "x", "y")).not.toThrow();
    expect(() => clearDraft(broken, "x")).not.toThrow();
  });
});
