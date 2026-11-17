import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./errors";
import {
  fetchSettings,
  normalizeSettings,
  saveSettings,
} from "./settings";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const BODY = {
  v: 1,
  fields: {
    voice: {
      env: "GEMINI_VOICE",
      kind: "string",
      value: "Leda",
      default: "Leda",
      source: "default",
      editable: true,
      read_only: false,
      pending: false,
      pending_value: null,
      minimum: null,
      maximum: null,
      choices: null,
    },
    console_port: {
      env: "ATHENA_CONSOLE_PORT",
      kind: "int",
      value: "8767",
      default: "8767",
      source: "store",
      editable: true,
      read_only: false,
      pending: true,
      pending_value: "8797",
      minimum: 1,
      maximum: 65535,
      choices: null,
    },
    memory_db: {
      env: "ATHENA_MEMORY_DB",
      kind: "string",
      value: "athena_memory.db",
      default: "athena_memory.db",
      source: "default",
      editable: false,
      read_only: true,
      pending: false,
      pending_value: null,
      minimum: null,
      maximum: null,
      choices: null,
    },
    log_level: {
      env: "LOG_LEVEL",
      kind: "string",
      value: "INFO",
      default: "INFO",
      source: "env",
      editable: true,
      read_only: false,
      pending: false,
      pending_value: null,
      minimum: null,
      maximum: null,
      choices: ["DEBUG", "INFO", "WARNING"],
    },
  },
  pending: [
    { key: "console_port", active: "8767", stored: "8797" },
  ],
  restart_required: true,
  restart: {
    required: true,
    automatic: false,
    requested: true,
    command: "docker restart athena",
  },
  surfaces: [
    {
      name: "console",
      host: "console_host",
      port: "console_port",
      scheme: "http",
      url: "http://127.0.0.1:8767",
    },
  ],
  secrets: [
    { name: "GEMINI_API_KEY", configured: true, updated_at: 1_760_000_000 },
  ],
  secrets_unreadable: false,
};

describe("normalizeSettings", () => {
  it("rend chaque champ avec sa valeur effective, sa source et son attente", () => {
    const settings = normalizeSettings(BODY);

    expect(settings.fields.map((field) => field.key)).toEqual([
      "voice",
      "console_port",
      "memory_db",
      "log_level",
    ]);
    expect(settings.fields[0]).toMatchObject({
      value: "Leda",
      source: "default",
      editable: true,
      readOnly: false,
    });
    expect(settings.fields[1]).toMatchObject({
      value: "8767",
      pending: true,
      pendingValue: "8797",
      kind: "int",
      minimum: 1,
      maximum: 65535,
    });
    expect(settings.fields[2]).toMatchObject({
      editable: false,
      readOnly: true,
    });
    // Un champ masqué par l'environnement est toujours en lecture seule.
    expect(settings.fields[3]).toMatchObject({
      source: "env",
      readOnly: true,
      choices: ["DEBUG", "INFO", "WARNING"],
    });
  });

  it("rend le redémarrage, les surfaces et les Secrets", () => {
    const settings = normalizeSettings(BODY);

    expect(settings.restart).toEqual({
      required: true,
      automatic: false,
      requested: true,
      command: "docker restart athena",
    });
    expect(settings.surfaces).toEqual([
      {
        name: "console",
        hostKey: "console_host",
        portKey: "console_port",
        scheme: "http",
        url: "http://127.0.0.1:8767",
      },
    ]);
    expect(settings.secrets).toEqual([
      {
        name: "GEMINI_API_KEY",
        configured: true,
        updatedAt: 1_760_000_000,
      },
    ]);
    expect(settings.secretsUnreadable).toBe(false);
  });

  it("ignore un champ sans nom et retombe sur des valeurs sûres", () => {
    const settings = normalizeSettings({
      fields: {
        voice: { source: "futur", kind: "inconnu" },
        "": { value: "orphelin" },
      },
      restart: { automatic: "oui" },
      surfaces: [{ name: "console" }, null],
      secrets: [{ configured: true }, { name: "GEMINI_API_KEY" }],
    });

    expect(settings.fields).toHaveLength(1);
    expect(settings.fields[0]).toMatchObject({
      key: "voice",
      kind: "string",
      source: "default",
      editable: true,
      readOnly: false,
    });
    expect(settings.restart).toMatchObject({
      required: false,
      automatic: false,
      requested: false,
      command: "",
    });
    expect(settings.surfaces).toEqual([]);
    expect(settings.secrets).toEqual([
      { name: "GEMINI_API_KEY", configured: false, updatedAt: null },
    ]);
  });

  it("tolère un corps vide sans casser l'écran", () => {
    expect(normalizeSettings(null)).toEqual({
      fields: [],
      surfaces: [],
      restart: {
        required: false,
        automatic: false,
        requested: false,
        command: "",
      },
      secrets: [],
      secretsUnreadable: false,
    });
  });
});

describe("fetchSettings", () => {
  it("lit les réglages sur leur route dédiée", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, BODY),
    );

    const settings = await fetchSettings(fetcher);

    expect(settings.fields).toHaveLength(4);
    const [path] = (fetcher.mock.calls[0] ?? []) as [string, RequestInit];
    expect(path).toBe("/api/settings");
  });
});

describe("saveSettings", () => {
  it("envoie l'objet complet et la confirmation des nouvelles adresses", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, BODY),
    );

    await saveSettings(
      { voice: "Puck", console_port: "8797" },
      ["http://127.0.0.1:8797"],
      fetcher,
    );

    const [path, init] = (fetcher.mock.calls[0] ?? []) as [string, RequestInit];
    expect(path).toBe("/api/settings");
    expect(init).toMatchObject({ method: "PUT" });
    expect(JSON.parse(String(init.body))).toEqual({
      values: { voice: "Puck", console_port: "8797" },
      confirm_url: ["http://127.0.0.1:8797"],
    });
  });

  it("n'envoie aucune confirmation quand rien ne change d'adresse", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, BODY),
    );

    await saveSettings({ voice: "Puck" }, undefined, fetcher);

    const [, init] = (fetcher.mock.calls[0] ?? []) as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      values: { voice: "Puck" },
    });
  });

  it("laisse les erreurs par champ remonter avec le corps de l'erreur", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(400, {
        v: 1,
        error: "invalid_settings",
        message: "Certains réglages sont invalides.",
        fields: { recall_k: "La valeur minimale est 1." },
      }),
    );

    const error = await saveSettings({ recall_k: "-1" }, undefined, fetcher).catch(
      (reason: unknown) => reason,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 400,
      code: "invalid_settings",
      fields: { recall_k: "La valeur minimale est 1." },
    });
  });
});
