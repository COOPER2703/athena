import { describe, expect, it } from "vitest";

import {
  formatPluginState,
  instructionNotice,
  isPluginState,
  normalizePlugin,
  normalizePlugins,
  pluginSheet,
  pluginStateClass,
  type PluginInfo,
} from "./plugins";

const websearch = {
  name: "websearch",
  state: "inactive",
  tools: ["web_search"],
  declaration: {
    description: "Cherche sur le web via Tavily",
    version: "1.0.0",
    api_version: 1,
    platform: "server",
    directory: "plugins/websearch",
    invalid_reason: null,
  },
  reason: null,
  retry: false,
  instruction: "",
};

describe("normalizePlugin", () => {
  it("reprend une entrée complète, en camelCase pour le front", () => {
    expect(normalizePlugin(websearch)).toEqual({
      name: "websearch",
      state: "inactive",
      tools: ["web_search"],
      declaration: {
        description: "Cherche sur le web via Tavily",
        version: "1.0.0",
        apiVersion: 1,
        platform: "server",
        directory: "plugins/websearch",
        invalidReason: null,
      },
      reason: null,
      retry: false,
      instruction: "",
    });
  });

  it("comble une entrée partielle sans inventer de Déclaration", () => {
    expect(normalizePlugin({ name: "notion" })).toEqual({
      name: "notion",
      state: "",
      tools: [],
      declaration: null,
      reason: null,
      retry: false,
      instruction: "",
    });
  });

  it("ignore les champs malformés et inconnus plutôt que de casser la fiche", () => {
    const plugin = normalizePlugin({
      name: 42,
      state: null,
      tools: ["a", 3, null],
      declaration: "pas une déclaration",
      reason: 42,
      retry: "oui",
      instruction: 7,
      secret: "ne-doit-pas-sortir",
    });

    expect(plugin).toEqual({
      name: "",
      state: "",
      tools: ["a"],
      declaration: null,
      reason: null,
      retry: false,
      instruction: "",
    });
    expect(plugin).not.toHaveProperty("secret");
  });

  it("normalise une Déclaration partielle et omet une api_version inconnue", () => {
    const plugin = normalizePlugin({
      name: "broken",
      declaration: {
        description: 7,
        version: "0.0.1",
        api_version: "1",
        platform: "desktop",
        directory: null,
        invalid_reason: "manifeste cassé",
      },
    });

    expect(plugin?.declaration).toEqual({
      description: "",
      version: "0.0.1",
      apiVersion: 0,
      platform: "desktop",
      directory: "",
      invalidReason: "manifeste cassé",
    });
  });

  it("rend null pour une entrée qui n'est pas un objet", () => {
    expect(normalizePlugin(null)).toBeNull();
    expect(normalizePlugin("websearch")).toBeNull();
    expect(normalizePlugins([websearch, null, "nope", 3])).toHaveLength(1);
    expect(normalizePlugins(null)).toEqual([]);
  });
});

describe("pluginSheet", () => {
  it("groupe l'identité, les Tools et l'Instruction d'un plugin déclaré", () => {
    const plugin = {
      ...(normalizePlugin(websearch) as PluginInfo),
      instruction: "Cherche avant de répondre.",
    };

    const sheet = pluginSheet(plugin);

    expect(sheet.identity).toEqual([
      { label: "Description", value: "Cherche sur le web via Tavily" },
      { label: "Version", value: "1.0.0", mono: true },
      { label: "api_version", value: "1", mono: true },
      { label: "Plateforme", value: "server" },
      { label: "Dossier", value: "plugins/websearch", mono: true },
    ]);
    expect(sheet.tools).toEqual(["web_search"]);
    expect(sheet.instruction).toBe("Cherche avant de répondre.");
    expect(sheet.failure).toBeNull();
    expect(sheet.invalidReason).toBeNull();
    expect(sheet.retry).toBe(false);
    expect(sheet.loaded).toBe(false);
    expect(sheet.declarationMissing).toBe(false);
  });

  it("omet api_version quand elle vaut zéro plutôt que d'afficher une fausse valeur", () => {
    const sheet = pluginSheet(
      normalizePlugin({
        name: "broken",
        state: "invalid",
        declaration: {
          description: "",
          version: "",
          api_version: 0,
          platform: "",
          directory: "plugins/broken",
          invalid_reason: "manifeste cassé",
        },
        reason: "manifeste cassé",
      }) as PluginInfo,
    );

    expect(sheet.identity.map((fact) => fact.label)).not.toContain("api_version");
    expect(sheet.invalidReason).toBe("manifeste cassé");
    expect(sheet.failure).toBe("manifeste cassé");
  });

  it("dit qu'une Déclaration manque quand rien n'a pu être lu", () => {
    const sheet = pluginSheet(
      normalizePlugin({ name: "ghost", state: "missing" }) as PluginInfo,
    );

    expect(sheet.declarationMissing).toBe(true);
    expect(sheet.identity).toEqual([]);
    expect(sheet.tools).toEqual([]);
  });

  it("signale une retente en cours et un échec de service", () => {
    const sheet = pluginSheet(
      normalizePlugin({
        name: "failing",
        state: "failed",
        declaration: {
          description: "Plugin failing",
          version: "0.1.0",
          api_version: 1,
          platform: "server",
          directory: "plugins/failing",
        },
        reason: "PluginUnavailable: injoignable",
        retry: true,
      }) as PluginInfo,
    );

    expect(sheet.failure).toBe("PluginUnavailable: injoignable");
    expect(sheet.retry).toBe(true);
    expect(sheet.loaded).toBe(false);
  });
});

describe("instructionNotice", () => {
  it("ne remplace jamais une Instruction fournie", () => {
    const plugin = {
      ...(normalizePlugin(websearch) as PluginInfo),
      state: "activated",
      instruction: "Cherche.",
    };
    expect(instructionNotice(plugin)).toBeNull();
  });

  it("dit explicitement qu'un plugin non chargé n'a pas d'Instruction", () => {
    const plugin = normalizePlugin(websearch) as PluginInfo;
    expect(instructionNotice(plugin)).toBe(
      "Aucune Instruction : ce plugin n'est pas chargé.",
    );
  });

  it("distingue un plugin chargé qui n'en fournit pas", () => {
    const plugin = {
      ...(normalizePlugin(websearch) as PluginInfo),
      state: "activated",
    };
    expect(instructionNotice(plugin)).toBe(
      "Ce plugin est chargé mais ne fournit aucune Instruction.",
    );
  });
});

describe("plugin state labels", () => {
  it("traduit les six états du Plugin Registry", () => {
    expect(formatPluginState("activated")).toBe("Activé");
    expect(formatPluginState("failed")).toBe("En échec");
    expect(formatPluginState("missing")).toBe("Manquant");
    expect(formatPluginState("inactive")).toBe("Inactif");
    expect(formatPluginState("unsupported")).toBe("Non supporté");
    expect(formatPluginState("invalid")).toBe("Invalide");
  });

  it("ne reconnaît plus l'ancien état disabled", () => {
    expect(isPluginState("disabled")).toBe(false);
    expect(formatPluginState("disabled")).toBe("disabled");
  });

  it("retombe sur l'état brut puis « Inconnu »", () => {
    expect(formatPluginState("paused")).toBe("paused");
    expect(formatPluginState("")).toBe("Inconnu");
  });

  it("donne une classe CSS bornée pour les états connus", () => {
    expect(pluginStateClass("activated")).toBe("activated");
    expect(pluginStateClass("unsupported")).toBe("unsupported");
    expect(pluginStateClass("disabled")).toBe("unknown");
    expect(pluginStateClass("paused")).toBe("unknown");
  });
});
