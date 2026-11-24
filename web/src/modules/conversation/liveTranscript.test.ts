import { describe, expect, it } from "vitest";

import type { ThreadEntry } from "../../api/thread";
import { mergeLiveTranscript } from "./liveTranscript";
import type { VoiceLine } from "./voice";

function journal(
  id: number,
  role: string,
  text: string,
  origin: ThreadEntry["origin"] = "voice",
): ThreadEntry {
  return {
    id,
    role,
    origin,
    text,
    createdAt: `2026-01-01T10:00:${String(id).padStart(2, "0")}Z`,
  };
}

const vous = (text: string): VoiceLine => ({ role: "user", text });
const athena = (text: string): VoiceLine => ({ role: "assistant", text });

describe("mergeLiveTranscript", () => {
  it("affiche le Journal puis les mots encore vivants, dans l'ordre", () => {
    const lines = mergeLiveTranscript(
      [
        journal(11, "user", "Bonjour Athena"),
        journal(12, "assistant", "Bonjour."),
      ],
      [vous("Comment vas-tu ?")],
      10,
    );

    expect(
      lines.map((line) => [line.role, line.text, line.entry === null]),
    ).toEqual([
      ["user", "Bonjour Athena", false],
      ["assistant", "Bonjour.", false],
      ["user", "Comment vas-tu ?", true],
    ]);
    expect(lines.map((line) => line.key)).toEqual([
      "journal-11",
      "journal-12",
      "live-0",
    ]);
  });

  it("n'affiche pas deux fois un tour déjà repris par le Journal", () => {
    const lines = mergeLiveTranscript(
      [journal(11, "user", "Bonjour"), journal(12, "assistant", "Salut.")],
      [vous("Bonjour"), athena("Salut.")],
      10,
    );

    expect(lines).toHaveLength(2);
    expect(lines.every((line) => line.entry !== null)).toBe(true);
  });

  it("garde un énoncé répété quand le Journal en contient déjà un", () => {
    // Deux « oui » successifs : le premier est journalisé, le second est encore
    // vivant. Une correspondance par simple présence les confondrait.
    const lines = mergeLiveTranscript(
      [journal(11, "user", "Oui")],
      [vous("Oui"), vous("Oui")],
      10,
    );

    expect(lines.map((line) => line.entry === null)).toEqual([false, true]);
  });

  it("ne confond pas un énoncé avec une entrée antérieure à la conversation", () => {
    // Une voix d'avant la conversation a dit « bonjour » ; la ligne vivante ne
    // doit pas disparaître : le repère de départ l'exclut de la déduplication.
    const lines = mergeLiveTranscript(
      [journal(4, "user", "Bonjour")],
      [vous("Bonjour")],
      9,
    );

    expect(lines).toHaveLength(2);
    expect(lines[1]).toMatchObject({ text: "Bonjour", entry: null });
  });

  it("ignore une entrée du Journal venue d'une autre origine", () => {
    // Un Tour clavier identique n'est pas la même parole que le micro : il ne
    // doit jamais absorber une ligne vivante (cohérence de l'origine).
    const lines = mergeLiveTranscript(
      [journal(11, "user", "Bonjour", "text")],
      [vous("Bonjour")],
      10,
    );

    expect(lines.map((line) => line.entry === null)).toEqual([false, true]);
  });

  it("garde des clés stables quand une ligne vivante rejoint le Journal", () => {
    const before = mergeLiveTranscript(
      [journal(11, "user", "Bonjour")],
      [vous("Bonjour"), athena("Salut.")],
      10,
    );
    const after = mergeLiveTranscript(
      [journal(11, "user", "Bonjour"), journal(12, "assistant", "Salut.")],
      [vous("Bonjour"), athena("Salut.")],
      10,
    );

    expect(before[0]?.key).toBe(after[0]?.key);
    expect(before[1]?.key).toBe("live-1");
    expect(after.map((line) => line.key)).toEqual(["journal-11", "journal-12"]);
  });
});
