import { describe, expect, it } from "vitest";

import {
  formatPluginState,
  normalizeStatus,
  pluginStateClass,
  statusFromHello,
} from "./status";
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
    { name: "homeassistant", state: "activated", tools: ["ha__turn_on"] },
    { name: "calendar", state: "failed", tools: [] },
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

describe("normalizeStatus", () => {
  it("reprend un instantané complet sans le modifier", () => {
    expect(normalizeStatus(snapshot)).toEqual(snapshot);
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
      plugins: [{ name: "homeassistant", tools: ["a", 3] }],
      config: { model: "gemini-live", secret: 42 },
    });
    expect(status.clients).toEqual([
      { client_id: "c-1", client_name: "", platform: "" },
    ]);
    expect(status.plugins).toEqual([
      { name: "homeassistant", state: "", tools: ["a"] },
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
    expect(statusFromHello(frame)).toEqual(snapshot);
  });
});

describe("plugin state labels", () => {
  it("traduit les états du Plugin Registry", () => {
    expect(formatPluginState("activated")).toBe("Activé");
    expect(formatPluginState("disabled")).toBe("Désactivé");
    expect(formatPluginState("missing")).toBe("Manquant");
    expect(formatPluginState("failed")).toBe("En échec");
  });

  it("retombe sur l'état brut puis « Inconnu »", () => {
    expect(formatPluginState("paused")).toBe("paused");
    expect(formatPluginState("")).toBe("Inconnu");
  });

  it("donne une classe CSS bornée pour les états connus", () => {
    expect(pluginStateClass("activated")).toBe("activated");
    expect(pluginStateClass("failed")).toBe("failed");
    expect(pluginStateClass("paused")).toBe("unknown");
  });
});
