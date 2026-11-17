import { describe, expect, it } from "vitest";

import {
  MUTED_PREFERENCE_KEY,
  readMuted,
  writeMuted,
  type PreferenceStorage,
} from "./preferences";

function storage(initial: Record<string, string> = {}): PreferenceStorage & {
  values: Map<string, string>;
} {
  const values = new Map(Object.entries(initial));
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => {
      values.set(key, value);
    },
  };
}

describe("sourdine", () => {
  it("est active par défaut, y compris sans stockage", () => {
    expect(readMuted(storage())).toBe(true);
    expect(readMuted(undefined)).toBe(true);
  });

  it("retient l'activation du son d'une visite à l'autre", () => {
    const prefs = storage();
    writeMuted(prefs, false);

    expect(prefs.values.get(MUTED_PREFERENCE_KEY)).toBe("0");
    expect(readMuted(prefs)).toBe(false);
  });

  it("retient aussi le retour à la sourdine", () => {
    const prefs = storage({ [MUTED_PREFERENCE_KEY]: "0" });
    writeMuted(prefs, true);

    expect(readMuted(prefs)).toBe(true);
  });

  it("ne casse jamais l'écran quand le stockage refuse", () => {
    const refused: PreferenceStorage = {
      getItem: () => {
        throw new Error("refusé");
      },
      setItem: () => {
        throw new Error("refusé");
      },
    };

    expect(readMuted(refused)).toBe(true);
    expect(() => writeMuted(refused, false)).not.toThrow();
  });
});
