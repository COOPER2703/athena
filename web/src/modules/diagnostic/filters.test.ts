import { describe, expect, it } from "vitest";

import {
  EMPTY_FILTERS,
  LIVE_VIEW,
  availableLevels,
  availableSources,
  displayedEntries,
  filterEntries,
  matchesFilters,
  normalizeText,
  pauseView,
  resumeView,
  viewAfterHello,
} from "./filters";
import type { TraceEntry } from "./stream";

function entry(overrides: Partial<TraceEntry> = {}): TraceEntry {
  return {
    timestamp: 1_700_000_000,
    source: "gemini",
    kind: "LlmFailed",
    level: "error",
    payload: { cause: "Déconnexion" },
    ...overrides,
  };
}

const ENTRIES = [
  entry({ source: "gemini", level: "error", kind: "LlmFailed" }),
  entry({ source: "app", level: "info", kind: "started", payload: {} }),
  entry({
    source: "consolidation",
    level: "warning",
    kind: "Retard",
    payload: {},
  }),
];

describe("filtres de traces", () => {
  it("garde tout quand aucun filtre n'est actif", () => {
    expect(filterEntries(ENTRIES, EMPTY_FILTERS)).toEqual(ENTRIES);
  });

  it("filtre par niveau puis par source", () => {
    expect(filterEntries(ENTRIES, { ...EMPTY_FILTERS, level: "error" })).toEqual(
      [ENTRIES[0]],
    );
    expect(filterEntries(ENTRIES, { ...EMPTY_FILTERS, source: "app" })).toEqual([
      ENTRIES[1],
    ]);
  });

  it("cherche dans la source, le kind et le payload, sans accents", () => {
    expect(
      filterEntries(ENTRIES, { ...EMPTY_FILTERS, text: "deconnexion" }),
    ).toEqual([ENTRIES[0]]);
    expect(
      filterEntries(ENTRIES, { ...EMPTY_FILTERS, text: "CONSOLIDATION" }),
    ).toEqual([ENTRIES[2]]);
    expect(filterEntries(ENTRIES, { ...EMPTY_FILTERS, text: "introuvable" })).toEqual(
      [],
    );
  });

  it("combine les trois filtres", () => {
    expect(
      matchesFilters(ENTRIES[1] as TraceEntry, {
        level: "info",
        source: "app",
        text: "started",
      }),
    ).toBe(true);
    expect(
      matchesFilters(ENTRIES[1] as TraceEntry, {
        level: "error",
        source: "app",
        text: "",
      }),
    ).toBe(false);
  });

  it("normalise casse et accents", () => {
    expect(normalizeText("Déconnexion Réseau")).toBe("deconnexion reseau");
  });

  it("liste les niveaux et sources présents, triés et dédupliqués", () => {
    const duplicated = [...ENTRIES, entry({ source: "app", level: "info" })];
    expect(availableLevels(duplicated)).toEqual(["error", "info", "warning"]);
    expect(availableSources(duplicated)).toEqual([
      "app",
      "consolidation",
      "gemini",
    ]);
  });
});

describe("pause de la vue", () => {
  it("gèle les traces présentes sans perdre le flux", () => {
    const view = pauseView(ENTRIES);
    expect(view.paused).toBe(true);
    expect(view.snapshot).toEqual(ENTRIES);

    const next: TraceEntry[] = [...ENTRIES, entry({ kind: "après" })];
    expect(displayedEntries(next, EMPTY_FILTERS, view)).toEqual(ENTRIES);
    expect(displayedEntries(next, EMPTY_FILTERS, resumeView())).toEqual(next);
  });

  it("applique encore les filtres à la vue gelée", () => {
    const view = pauseView(ENTRIES);
    expect(displayedEntries(ENTRIES, { ...EMPTY_FILTERS, source: "app" }, view))
      .toEqual([ENTRIES[1]]);
  });

  it("repart du nouveau backlog au hello, en conservant la pause", () => {
    const paused = viewAfterHello(pauseView(ENTRIES), [ENTRIES[1] as TraceEntry]);
    expect(paused.paused).toBe(true);
    expect(paused.snapshot).toEqual([ENTRIES[1]]);

    expect(viewAfterHello(LIVE_VIEW, ENTRIES)).toBe(LIVE_VIEW);
  });
});
