import { describe, expect, it } from "vitest";

import {
  EMPTY_PLUGIN_FILTER,
  PLUGIN_STATE_FILTERS,
  filterPlugins,
  normalizePlugin,
  pluginCounterItems,
  pluginCounters,
  pluginFilterActive,
  pluginRailMotif,
  pluginResultSummary,
  pluginRow,
  type PluginInfo,
} from "./plugins";

function plugin(raw: Record<string, unknown>): PluginInfo {
  return normalizePlugin(raw) as PluginInfo;
}

const activated = plugin({
  name: "homeassistant",
  state: "activated",
  tools: ["turn_on", "turn_off"],
  declaration: {
    description: "Pilote la domotique",
    version: "1.2.0",
    api_version: 1,
    platform: "server",
    directory: "plugins/homeassistant",
  },
});

const failed = plugin({
  name: "calendar",
  state: "failed",
  tools: [],
  declaration: {
    description: "Agenda partagé",
    version: "0.3.0",
    api_version: 1,
    platform: "server",
    directory: "plugins/calendar",
  },
  reason: "PluginUnavailable: injoignable",
  retry: true,
});

const missing = plugin({ name: "ghost", state: "missing" });

const inactive = plugin({
  name: "websearch",
  state: "inactive",
  tools: ["web_search"],
  declaration: {
    description: "Cherche sur le web via Tàvily",
    version: "1.0.0",
    api_version: 1,
    platform: "server",
    directory: "plugins/websearch",
  },
});

const unsupported = plugin({
  name: "mobile-shell",
  state: "unsupported",
  tools: [],
  declaration: {
    description: "Coque Android",
    version: "0.1.0",
    api_version: 1,
    platform: "android",
    directory: "plugins/mobile-shell",
  },
});

const invalid = plugin({
  name: "broken",
  state: "invalid",
  tools: [],
  declaration: {
    platform: "server",
    directory: "plugins/broken",
    invalid_reason: "manifest is not valid JSON",
  },
});

/** Un plugin par État de service, dans l'ordre du flux. */
const registry = [activated, failed, missing, inactive, unsupported, invalid];

describe("pluginCounters", () => {
  it("compte les plugins découverts, en service et en échec", () => {
    expect(pluginCounters(registry)).toEqual({
      discovered: 6,
      activated: 1,
      failed: 1,
    });
  });

  it("ne compte comme en service que l'état activated, pas missing ni inactive", () => {
    const counters = pluginCounters([
      plugin({ name: "a", state: "activated" }),
      plugin({ name: "b", state: "missing" }),
      plugin({ name: "c", state: "inactive" }),
    ]);
    expect(counters).toEqual({ discovered: 3, activated: 1, failed: 0 });
  });

  it("rend des compteurs nuls pour un registre vide", () => {
    expect(pluginCounters([])).toEqual({
      discovered: 0,
      activated: 0,
      failed: 0,
    });
    expect(pluginCounterItems(pluginCounters([]))).toEqual([
      { key: "discovered", label: "Découverts", value: 0 },
      { key: "activated", label: "En service", value: 0 },
      { key: "failed", label: "En échec", value: 0 },
    ]);
  });
});

describe("plugin search", () => {
  it("rapproche le nom sans tenir compte de la casse", () => {
    const found = filterPlugins(registry, {
      state: "",
      text: "  WEBSEARCH ",
    });
    expect(found.map((item) => item.name)).toEqual(["websearch"]);
  });

  it("rapproche la description sans tenir compte des accents", () => {
    const found = filterPlugins(registry, {
      state: "",
      text: "tavily",
    });
    expect(found.map((item) => item.name)).toEqual(["websearch"]);

    expect(
      filterPlugins(registry, { state: "", text: "agenda partage" }).map(
        (item) => item.name,
      ),
    ).toEqual(["calendar"]);
  });

  it("rapproche un nom de Tool, casse et accents ignorés", () => {
    expect(
      filterPlugins(registry, { state: "", text: "TURN_ON" }).map(
        (item) => item.name,
      ),
    ).toEqual(["homeassistant"]);
  });

  it("ne rapproche ni l'état, ni le dossier, ni la raison d'échec", () => {
    expect(filterPlugins(registry, { state: "", text: "failed" })).toEqual([]);
    expect(filterPlugins(registry, { state: "", text: "plugins/broken" })).toEqual(
      [],
    );
    expect(
      filterPlugins(registry, { state: "", text: "injoignable" }),
    ).toEqual([]);
  });

  it("rend tout le registre pour une recherche vide", () => {
    expect(filterPlugins(registry, EMPTY_PLUGIN_FILTER)).toHaveLength(6);
    expect(filterPlugins(registry, { state: "", text: "   " })).toHaveLength(6);
  });
});

describe("plugin state filter", () => {
  it("ne garde que l'état demandé", () => {
    expect(
      filterPlugins(registry, { state: "failed", text: "" }).map(
        (item) => item.name,
      ),
    ).toEqual(["calendar"]);
  });

  it("combine l'état et la recherche", () => {
    expect(
      filterPlugins(registry, { state: "inactive", text: "tavily" }).map(
        (item) => item.name,
      ),
    ).toEqual(["websearch"]);
    expect(
      filterPlugins(registry, { state: "failed", text: "tavily" }),
    ).toEqual([]);
  });

  it("ne considère le filtre actif que sur un état connu ou un texte réel", () => {
    expect(pluginFilterActive(EMPTY_PLUGIN_FILTER)).toBe(false);
    expect(pluginFilterActive({ state: "", text: "   " })).toBe(false);
    expect(pluginFilterActive({ state: "failed", text: "" })).toBe(true);
    expect(pluginFilterActive({ state: "", text: "web" })).toBe(true);
  });
});

describe("pluginResultSummary", () => {
  it("dit explicitement qu'aucun plugin n'est installé", () => {
    expect(
      pluginResultSummary(EMPTY_PLUGIN_FILTER, { total: 0, shown: 0 }),
    ).toEqual({ label: "Aucun Plugin installé.", empty: true });
  });

  it("dit explicitement qu'aucun plugin ne correspond aux filtres", () => {
    const result = pluginResultSummary(
      { state: "failed", text: "tavily" },
      { total: 6, shown: 0 },
    );
    expect(result.empty).toBe(true);
    expect(result.label).toBe(
      "Aucun Plugin ne correspond à la recherche et au filtre d'état.",
    );
  });

  it("compte les plugins affichés quand un filtre est actif", () => {
    expect(
      pluginResultSummary({ state: "inactive", text: "" }, {
        total: 6,
        shown: 1,
      }),
    ).toEqual({ label: "1 Plugin affiché sur 6.", empty: false });
    expect(
      pluginResultSummary({ state: "", text: "e" }, { total: 6, shown: 4 }),
    ).toEqual({ label: "4 Plugins affichés sur 6.", empty: false });
  });

  it("énonce le total installé sans filtre", () => {
    expect(
      pluginResultSummary(EMPTY_PLUGIN_FILTER, { total: 6, shown: 6 }),
    ).toEqual({ label: "6 Plugins installés.", empty: false });
    expect(
      pluginResultSummary(EMPTY_PLUGIN_FILTER, { total: 1, shown: 1 }),
    ).toEqual({ label: "1 Plugin installé.", empty: false });
  });
});

describe("pluginRailMotif", () => {
  it("donne un motif distinct aux six états", () => {
    const motifs = [
      pluginRailMotif("activated"),
      pluginRailMotif("inactive"),
      pluginRailMotif("missing"),
      pluginRailMotif("failed"),
      pluginRailMotif("unsupported"),
      pluginRailMotif("invalid"),
    ];
    expect(motifs).toEqual([
      "solid",
      "hollow",
      "dotted",
      "broken",
      "hatched",
      "double",
    ]);
    expect(new Set(motifs).size).toBe(6);
  });

  it("retombe sur un motif sûr pour un état inconnu, sans injecter l'état brut", () => {
    expect(pluginRailMotif("")).toBe("hollow");
    expect(pluginRailMotif("paused")).toBe("hollow");
  });

  it("expose les six états et « tous » comme options de filtre", () => {
    expect(PLUGIN_STATE_FILTERS).toEqual([
      { value: "", label: "Tous les états" },
      { value: "activated", label: "Activé" },
      { value: "failed", label: "En échec" },
      { value: "missing", label: "Manquant" },
      { value: "inactive", label: "Inactif" },
      { value: "unsupported", label: "Non supporté" },
      { value: "invalid", label: "Invalide" },
    ]);
  });
});

describe("pluginRow", () => {
  it("regroupe ce qu'une ligne affiche : nom, état, motif, description, Tools", () => {
    expect(pluginRow(activated)).toEqual({
      name: "homeassistant",
      nameLabel: "homeassistant",
      stateLabel: "Activé",
      stateClass: "activated",
      motif: "solid",
      descriptionLabel: "Pilote la domotique",
      toolsLabel: "2 Tools",
    });
  });

  it("nomme explicitement une ligne sans nom, sans description ou sans Tool", () => {
    const row = pluginRow(missing);
    expect(row.nameLabel).toBe("ghost");
    expect(row.descriptionLabel).toBe("Aucune description déclarée.");
    expect(row.toolsLabel).toBe("Aucun Tool");

    expect(pluginRow(plugin({ name: "" })).nameLabel).toBe("(sans nom)");
    expect(pluginRow(plugin({ name: "one", tools: ["t"] })).toolsLabel).toBe(
      "1 Tool",
    );
  });

  it("porte le libellé de l'état et son motif ensemble", () => {
    expect(pluginRow(invalid)).toMatchObject({
      stateLabel: "Invalide",
      motif: "double",
    });
    expect(pluginRow(unsupported)).toMatchObject({
      stateLabel: "Non supporté",
      motif: "hatched",
    });
  });
});
