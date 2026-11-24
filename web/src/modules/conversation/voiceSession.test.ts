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

  it("ignore la Session portée par le Client Console", () => {
    // La Console ne doit pas s'avertir elle-même d'une « Session vocale en
    // cours » : l'instantané dit que le Client Console la porte (ADR-0027).
    expect(
      voiceSessionActive({
        session: {
          state: "SessionVisible",
          session_id: "abc",
          console: true,
        },
      }),
    ).toBe(false);
  });

  it("garde l'avertissement pour un Client desktop", () => {
    expect(
      voiceSessionActive({
        session: {
          state: "SessionVisible",
          session_id: "abc",
          console: false,
        },
      }),
    ).toBe(true);
  });

  it("reste faux tant que la Session n'est pas visible", () => {
    // `console` vaut vrai dès l'acceptation de la WebSocket, avant que la
    // Session n'ait un identifiant : il n'y a encore rien à signaler.
    expect(
      voiceSessionActive({
        session: { state: "LiveConnecting", session_id: "", console: true },
      }),
    ).toBe(false);
  });

  it("reste faux sur un instantané absent ou mal formé", () => {
    expect(voiceSessionActive(null)).toBe(false);
    expect(voiceSessionActive({})).toBe(false);
    expect(voiceSessionActive({ session: { session_id: 42 } })).toBe(false);
  });
});
