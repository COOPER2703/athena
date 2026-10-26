import { describe, expect, it } from "vitest";

import {
  EMPTY_FILTERS,
  LIVE_VIEW,
  availableLevels,
  availableSources,
  displayedEntries,
  filterEntries,
  pauseView,
  resumeView,
  type TraceFilters,
} from "./filters";
import type { TraceEntry } from "./trace";

function entry(overrides: Partial<TraceEntry> = {}): TraceEntry {
  return {
    timestamp: 1_700_000_000,
    source: "core",
    kind: "Started",
    level: "info",
    payload: {},
    ...overrides,
  };
}

const entries: TraceEntry[] = [
  entry({ level: "info", source: "core", kind: "Started", payload: { state: "Idle" } }),
  entry({
    level: "debug",
    source: "gemini",
    kind: "LlmOpened",
    payload: { connection_id: "c1" },
  }),
  entry({
    level: "warning",
    source: "logging",
    kind: "Avertissement",
    payload: { message: "Porte Ouverte" },
  }),
  entry({
    level: "debug",
    source: "core",
    kind: "Déconnexion",
    payload: { reason: "réseau" },
  }),
];

function filters(overrides: Partial<TraceFilters> = {}): TraceFilters {
  return { ...EMPTY_FILTERS, ...overrides };
}

describe("filterEntries", () => {
  it("garde toutes les traces sans filtre", () => {
    expect(filterEntries(entries, EMPTY_FILTERS)).toEqual(entries);
  });

  it("filtre par niveau exact", () => {
    expect(
      filterEntries(entries, filters({ level: "debug" })).map((item) => item.kind),
    ).toEqual(["LlmOpened", "Déconnexion"]);
  });

  it("filtre par source exacte", () => {
    expect(
      filterEntries(entries, filters({ source: "core" })).map((item) => item.kind),
    ).toEqual(["Started", "Déconnexion"]);
  });

  it("cherche dans la source, le kind et le payload sérialisé", () => {
    expect(filterEntries(entries, filters({ text: "gemini" }))).toHaveLength(1);
    expect(
      filterEntries(entries, filters({ text: "llm" })).map((item) => item.kind),
    ).toEqual(["LlmOpened"]);
    expect(
      filterEntries(entries, filters({ text: "connection_id" })).map(
        (item) => item.kind,
      ),
    ).toEqual(["LlmOpened"]);
  });

  it("ignore la casse et les accents", () => {
    expect(
      filterEntries(entries, filters({ text: "DECONNEXION" })).map(
        (item) => item.kind,
      ),
    ).toEqual(["Déconnexion"]);
    expect(
      filterEntries(entries, filters({ text: "reseau" })).map((item) => item.kind),
    ).toEqual(["Déconnexion"]);
    expect(
      filterEntries(entries, filters({ text: "porte ouverte" })).map(
        (item) => item.kind,
      ),
    ).toEqual(["Avertissement"]);
  });

  it("combine niveau, source et texte", () => {
    expect(
      filterEntries(
        entries,
        filters({ level: "debug", source: "core", text: "reseau" }),
      ).map((item) => item.kind),
    ).toEqual(["Déconnexion"]);

    expect(
      filterEntries(
        entries,
        filters({ level: "info", source: "core", text: "reseau" }),
      ),
    ).toEqual([]);
  });

  it("ignore un texte fait uniquement d'espaces", () => {
    expect(filterEntries(entries, filters({ text: "   " }))).toEqual(entries);
  });
});

describe("availableLevels and availableSources", () => {
  it("déduit les valeurs présentes, triées et sans doublon", () => {
    expect(availableLevels(entries)).toEqual(["debug", "info", "warning"]);
    expect(availableSources(entries)).toEqual(["core", "gemini", "logging"]);
    expect(availableLevels([])).toEqual([]);
    expect(availableSources([])).toEqual([]);
  });
});

describe("pause", () => {
  it("gèle la vue sans perdre le flux qui continue d'arriver", () => {
    const view = pauseView(entries);
    const later = [...entries, entry({ kind: "ArrivéeAprèsPause" })];

    expect(displayedEntries(later, EMPTY_FILTERS, view)).toEqual(entries);
    expect(displayedEntries(later, EMPTY_FILTERS, resumeView())).toEqual(later);
  });

  it("reste filtrée pendant la pause", () => {
    const view = pauseView(entries);
    expect(
      displayedEntries(
        [...entries, entry({ kind: "Après" })],
        filters({ level: "debug" }),
        view,
      ).map((item) => item.kind),
    ).toEqual(["LlmOpened", "Déconnexion"]);
  });

  it("part de la vue vivante par défaut", () => {
    expect(LIVE_VIEW.paused).toBe(false);
    expect(displayedEntries(entries, EMPTY_FILTERS, LIVE_VIEW)).toEqual(entries);
  });
});
