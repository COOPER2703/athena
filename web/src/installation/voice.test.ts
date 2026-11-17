import { describe, expect, it } from "vitest";

import type { SettingField } from "../api/settings";
import {
  voiceDraft,
  voiceErrors,
  voiceFields,
  voicePayload,
  voiceRows,
} from "./voice";

function field(
  key: string,
  value: string,
  overrides: Partial<SettingField> = {},
): SettingField {
  return {
    key,
    env: key.toUpperCase(),
    kind: "string",
    value,
    defaultValue: value,
    source: "default",
    editable: true,
    readOnly: false,
    pending: false,
    pendingValue: null,
    minimum: null,
    maximum: null,
    choices: [],
    ...overrides,
  };
}

const FIELDS: SettingField[] = [
  field("voice", "Leda"),
  field("live_model", "gemini-live"),
  field("judge_model", "gemini-judge"),
  field("console_host", "127.0.0.1"),
  field("memory_db", "athena.db", { editable: false, readOnly: true }),
];

describe("voiceFields", () => {
  it("ne garde que la voix et les modèles, s'ils sont écrivables", () => {
    expect(voiceFields(FIELDS).map((item) => item.key)).toEqual([
      "voice",
      "live_model",
      "judge_model",
    ]);
    const masked = FIELDS.map((item) =>
      item.key === "voice"
        ? { ...item, source: "env" as const, readOnly: true }
        : item,
    );
    expect(voiceFields(masked).map((item) => item.key)).toEqual([
      "live_model",
      "judge_model",
    ]);
  });
});

describe("voiceRows", () => {
  it("affiche la valeur effective du serveur, jamais une constante", () => {
    expect(voiceRows(FIELDS, voiceDraft(FIELDS))).toEqual([
      { key: "voice", label: "Voix", value: "Leda" },
      { key: "live_model", label: "Modèle Live", value: "gemini-live" },
      { key: "judge_model", label: "Modèle du juge", value: "gemini-judge" },
    ]);
  });
});

describe("voicePayload", () => {
  it("envoie l'objet complet en ne changeant que la voix choisie", () => {
    const draft = { ...voiceDraft(FIELDS), voice: "Puck" };
    const payload = voicePayload(FIELDS, draft);

    expect(payload.voice).toBe("Puck");
    expect(payload.live_model).toBe("gemini-live");
    expect(payload.console_host).toBe("127.0.0.1");
    // Un chemin protégé ne part jamais dans l'écriture.
    expect(payload).not.toHaveProperty("memory_db");
  });

  it("accepte le brouillon inchangé sans erreur", () => {
    const draft = voiceDraft(FIELDS);
    expect(voiceErrors(FIELDS, draft)).toEqual({});
  });
});
