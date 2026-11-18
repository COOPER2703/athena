import { describe, expect, it, vi } from "vitest";

import {
  deletePluginSecret,
  fetchPlugins,
  normalizePlugins,
  setPluginSecret,
  updatePlugin,
} from "./plugins";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("normalizePlugins", () => {
  it("lit l'état, le manifeste et les champs déclarés", () => {
    const payload = normalizePlugins({
      v: 1,
      restart_required: true,
      plugins: [
        {
          name: "notion",
          enabled: true,
          state: "activated",
          version: "0.1.0",
          platform: "server",
          description: "Pages Notion",
          tools: ["read_page", "search_pages"],
          instruction: "Utilise read_page.",
          config: [
            {
              key: "parent_page_id",
              type: "string",
              required: true,
              value: "abc",
            },
          ],
          secrets: [
            {
              key: "token",
              required: true,
              variable: "ATHENA_PLUGIN_NOTION_TOKEN",
              configured: true,
              updated_at: 1_700_000_000,
            },
          ],
          pending_restart: true,
          error: "",
        },
      ],
    });

    expect(payload.restartRequired).toBe(true);
    const [plugin] = payload.plugins;
    expect(plugin).toMatchObject({
      name: "notion",
      enabled: true,
      state: "activated",
      version: "0.1.0",
      description: "Pages Notion",
      tools: ["read_page", "search_pages"],
      pendingRestart: true,
    });
    expect(plugin?.config[0]).toEqual({
      key: "parent_page_id",
      type: "string",
      required: true,
      value: "abc",
    });
    expect(plugin?.secrets[0]).toEqual({
      key: "token",
      required: true,
      variable: "ATHENA_PLUGIN_NOTION_TOKEN",
      configured: true,
      updatedAt: 1_700_000_000,
    });
  });

  it("retombe sur des valeurs sûres pour un corps mal formé", () => {
    expect(normalizePlugins(null)).toEqual({
      plugins: [],
      restartRequired: false,
    });
    expect(normalizePlugins({ plugins: "non" })).toEqual({
      plugins: [],
      restartRequired: false,
    });

    const payload = normalizePlugins({
      plugins: [
        { state: "état-inconnu", config: {}, secrets: null },
        { name: "   " },
        {
          name: "casse",
          state: "inconnu",
          config: [{ key: "url", type: "inconnu", value: 42 }],
          secrets: [{ key: "token", configured: "oui" }],
        },
      ],
    });

    expect(payload.plugins.map((plugin) => plugin.name)).toEqual(["casse"]);
    const [plugin] = payload.plugins;
    expect(plugin?.state).toBe("disabled");
    expect(plugin?.config[0]).toEqual({
      key: "url",
      type: "string",
      required: false,
      value: "",
    });
    expect(plugin?.secrets[0]).toMatchObject({
      configured: false,
      updatedAt: null,
    });
  });

  it("ne copie jamais une valeur de secret renvoyée par erreur", () => {
    const payload = normalizePlugins({
      plugins: [
        {
          name: "notion",
          secrets: [{ key: "token", value: "fuité", configured: true }],
        },
      ],
    });
    expect(payload.plugins[0]?.secrets[0]).not.toHaveProperty("value");
    expect(JSON.stringify(payload)).not.toContain("fuité");
  });
});

describe("appels de la zone Plugins", () => {
  it("lit la liste sur /api/plugins", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1, plugins: [] }),
    );

    await fetchPlugins(fetcher);

    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/plugins");
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({
      credentials: "same-origin",
    });
  });

  it("envoie une activation et une configuration par PUT", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, {
        v: 1,
        plugin: { name: "notion", enabled: true, state: "disabled" },
        restart_required: true,
      }),
    );

    const result = await updatePlugin(
      "notion",
      { enabled: true, config: { parent_page_id: "abc" } },
      fetcher,
    );

    const [path, init] = fetcher.mock.calls[0] ?? [];
    expect(path).toBe("/api/plugins/notion");
    expect(init?.method).toBe("PUT");
    expect(init?.body).toBe(
      JSON.stringify({ enabled: true, config: { parent_page_id: "abc" } }),
    );
    expect(result.restartRequired).toBe(true);
    expect(result.plugin?.enabled).toBe(true);
  });

  it("encode le nom du Plugin et la clé du Secret dans le chemin", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1, secret: { key: "token" } }),
    );

    await setPluginSecret("google workspace", "clé", "valeur", fetcher);
    await deletePluginSecret("google workspace", "clé", fetcher);

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      "/api/plugins/google%20workspace/secrets/cl%C3%A9",
    );
    expect(fetcher.mock.calls[1]?.[0]).toBe(
      "/api/plugins/google%20workspace/secrets/cl%C3%A9",
    );
    expect(fetcher.mock.calls[1]?.[1]?.method).toBe("DELETE");
  });

  it("rend un Secret sans sa valeur après écriture", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, {
        v: 1,
        secret: {
          key: "token",
          required: true,
          variable: "ATHENA_PLUGIN_NOTION_TOKEN",
          configured: true,
          updated_at: 1_700_000_000,
        },
      }),
    );

    const secret = await setPluginSecret("notion", "token", "valeur", fetcher);

    expect(secret).toEqual({
      key: "token",
      required: true,
      variable: "ATHENA_PLUGIN_NOTION_TOKEN",
      configured: true,
      updatedAt: 1_700_000_000,
    });
    expect(secret).not.toHaveProperty("value");
  });
});
