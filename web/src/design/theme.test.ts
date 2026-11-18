import { describe, expect, it } from "vitest";

import {
  DEFAULT_THEME,
  initialTheme,
  nextTheme,
  parseTheme,
  themeLabel,
} from "./theme";

describe("thème", () => {
  it("démarre en sombre, même sur un système clair", () => {
    expect(DEFAULT_THEME).toBe("dark");
    expect(initialTheme(null)).toBe("dark");
    expect(initialTheme("inconnu")).toBe("dark");
  });

  it("relit un choix explicite", () => {
    expect(initialTheme("light")).toBe("light");
    expect(initialTheme("dark")).toBe("dark");
    expect(parseTheme("light")).toBe("light");
    expect(parseTheme(42)).toBeNull();
  });

  it("bascule entre les deux thèmes", () => {
    expect(nextTheme("dark")).toBe("light");
    expect(nextTheme("light")).toBe("dark");
  });

  it("nomme chaque thème en français", () => {
    expect(themeLabel("dark")).toBe("Sombre");
    expect(themeLabel("light")).toBe("Clair");
  });
});
