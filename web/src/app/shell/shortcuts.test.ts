import { describe, expect, it } from "vitest";

import {
  IDLE_SHORTCUT_STATE,
  isEditableTarget,
  isPaletteShortcut,
  reduceShortcut,
} from "./shortcuts";

describe("palette de commandes", () => {
  it("s'ouvre avec Cmd+K ou Ctrl+K", () => {
    expect(isPaletteShortcut({ key: "k", metaKey: true, ctrlKey: false })).toBe(
      true,
    );
    expect(isPaletteShortcut({ key: "K", metaKey: false, ctrlKey: true })).toBe(
      true,
    );
    expect(isPaletteShortcut({ key: "k", metaKey: false, ctrlKey: false })).toBe(
      false,
    );
    expect(isPaletteShortcut({ key: "p", metaKey: true, ctrlKey: false })).toBe(
      false,
    );
  });
});

describe("navigation par séquences g", () => {
  it("g puis une lettre ouvre la zone", () => {
    const primed = reduceShortcut(IDLE_SHORTCUT_STATE, "g");
    expect(primed.zone).toBeUndefined();
    expect(primed.state.chord).toBe(true);

    const pressed = reduceShortcut(primed.state, "p");
    expect(pressed.zone).toBe("plugins");
    expect(pressed.state.chord).toBe(false);
  });

  it("accepte les quatre zones", () => {
    for (const [key, zone] of [
      ["c", "conversation"],
      ["s", "settings"],
      ["p", "plugins"],
      ["d", "diagnostic"],
    ] as const) {
      expect(reduceShortcut({ chord: true }, key).zone).toBe(zone);
    }
  });

  it("oublie une séquence qui ne mène nulle part", () => {
    const pressed = reduceShortcut({ chord: true }, "x");
    expect(pressed.zone).toBeUndefined();
    expect(pressed.state.chord).toBe(false);
  });

  it("ignore une lettre qui ne suit pas g", () => {
    expect(reduceShortcut(IDLE_SHORTCUT_STATE, "p").zone).toBeUndefined();
  });
});

describe("isEditableTarget", () => {
  it("laisse le clavier aux champs de saisie", () => {
    expect(
      isEditableTarget({ tagName: "INPUT", isContentEditable: false }),
    ).toBe(true);
    expect(
      isEditableTarget({ tagName: "TEXTAREA", isContentEditable: false }),
    ).toBe(true);
    expect(
      isEditableTarget({ tagName: "SELECT", isContentEditable: false }),
    ).toBe(true);
    expect(isEditableTarget({ tagName: "DIV", isContentEditable: true })).toBe(
      true,
    );
    expect(isEditableTarget({ tagName: "DIV", isContentEditable: false })).toBe(
      false,
    );
    expect(isEditableTarget(null)).toBe(false);
  });
});
