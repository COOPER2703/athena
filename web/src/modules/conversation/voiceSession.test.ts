import { describe, expect, it } from "vitest";

import { voiceSessionActive } from "./voiceSession";

describe("voiceSessionActive", () => {
  it("reconnaît une Session vocale en cours", () => {
    expect(
      voiceSessionActive({
        session: { state: "SessionVisible", session_id: "abc" },
      }),
    ).toBe(true);
  });

  it("ne confond pas un Tour texte avec une Session vocale", () => {
    // Pendant un Tour, l'état peut être actif mais aucune Session n'existe.
    expect(
      voiceSessionActive({ session: { state: "LiveActive", session_id: "" } }),
    ).toBe(false);
  });

  it("reste faux sur un instantané absent ou mal formé", () => {
    expect(voiceSessionActive(null)).toBe(false);
    expect(voiceSessionActive({})).toBe(false);
    expect(voiceSessionActive({ session: { session_id: 42 } })).toBe(false);
  });
});
