import { describe, expect, it } from "vitest";

import { normalizeStatus, statusFromHello } from "./status";
import type { HelloFrame } from "./trace";

const snapshot = {
  session: {
    state: "SessionVisible",
    session_id: "sess-1",
    connection_id: "conn-1",
    active_client_id: "client-1",
  },
  clients: [
    { client_id: "client-1", client_name: "desktop", platform: "linux" },
  ],
  plugins: [
    {
      name: "homeassistant",
      state: "activated",
      tools: ["ha__turn_on"],
      declaration: {
        description: "Pilote la domotique",
        version: "0.2.0",
        api_version: 1,
        platform: "server",
        directory: "plugins/homeassistant",
        invalid_reason: null,
      },
      reason: null,
      retry: false,
      instruction: "Domotique.",
      config: [
        { key: "url", type: "url", required: true, value: "http://ha.local" },
      ],
      secrets: [
        {
          key: "token",
          variable: "ATHENA_PLUGIN_HOMEASSISTANT_TOKEN",
          required: true,
          present: true,
        },
      ],
      ready: { manifest: true, config: true, secrets: true },
    },
    {
      name: "calendar",
      state: "failed",
      tools: [],
      declaration: null,
      reason: "PluginUnavailable: calendrier injoignable",
      retry: true,
      instruction: "",
      config: [],
      secrets: [],
      ready: { manifest: false, config: true, secrets: true },
    },
  ],
  config: {
    model: "gemini-live",
    voice: "Aoede",
    user_id: "owner",
    memory_db: "athena_memory.db",
    log_dir: "logs",
    version: "0.1.0",
  },
};

const normalized = {
  ...snapshot,
  plugins: [
    {
      name: "homeassistant",
      state: "activated",
      tools: ["ha__turn_on"],
      declaration: {
        description: "Pilote la domotique",
        version: "0.2.0",
        apiVersion: 1,
        platform: "server",
        directory: "plugins/homeassistant",
        invalidReason: null,
      },
      reason: null,
      retry: false,
      instruction: "Domotique.",
      config: [
        { key: "url", type: "url", required: true, value: "http://ha.local" },
      ],
      secrets: [
        {
          key: "token",
          variable: "ATHENA_PLUGIN_HOMEASSISTANT_TOKEN",
          required: true,
          present: true,
        },
      ],
      ready: { manifest: true, config: true, secrets: true },
    },
    {
      name: "calendar",
      state: "failed",
      tools: [],
      declaration: null,
      reason: "PluginUnavailable: calendrier injoignable",
      retry: true,
      instruction: "",
      config: [],
      secrets: [],
      ready: { manifest: false, config: true, secrets: true },
    },
  ],
};

describe("normalizeStatus", () => {
  it("normalise un instantané complet, plugins compris", () => {
    expect(normalizeStatus(snapshot)).toEqual(normalized);
  });

  it("rend un état vide sûr quand les champs manquent", () => {
    expect(normalizeStatus({})).toEqual({
      session: {
        state: "",
        session_id: "",
        connection_id: "",
        active_client_id: "",
      },
      clients: [],
      plugins: [],
      config: {},
    });
    expect(normalizeStatus(null).clients).toEqual([]);
  });

  it("ignore les entrées mal formées plutôt que de casser la page", () => {
    const status = normalizeStatus({
      clients: [{ client_id: "c-1" }, "nope"],
      plugins: [{ name: "homeassistant", tools: ["a", 3] }, null],
      config: { model: "gemini-live", secret: 42 },
    });
    expect(status.clients).toEqual([
      { client_id: "c-1", client_name: "", platform: "" },
    ]);
    expect(status.plugins).toEqual([
      {
        name: "homeassistant",
        state: "",
        tools: ["a"],
        declaration: null,
        reason: null,
        retry: false,
        instruction: "",
        config: [],
        secrets: [],
        ready: null,
      },
    ]);
    expect(status.config).toEqual({ model: "gemini-live" });
  });
});

describe("statusFromHello", () => {
  it("remplace l'état affiché par l'instantané du hello", () => {
    const frame: HelloFrame = {
      v: 1,
      type: "hello",
      data: { snapshot, backlog: [] },
    };
    expect(statusFromHello(frame)).toEqual(normalized);
  });
});
