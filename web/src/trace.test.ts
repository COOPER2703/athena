import { describe, expect, it } from "vitest";

import {
  applyTrace,
  backoffDelay,
  connectionLabel,
  emptyFeed,
  formatPayload,
  formatTimestamp,
  isTruncation,
  resetOnHello,
  type HelloFrame,
  type TraceEntry,
} from "./trace";

function entry(
  kind: string,
  overrides: Partial<TraceEntry> = {},
): TraceEntry {
  return {
    timestamp: 1_700_000_000,
    source: "core",
    kind,
    level: "info",
    payload: {},
    ...overrides,
  };
}

function hello(backlog: TraceEntry[]): HelloFrame {
  return {
    v: 1,
    type: "hello",
    data: { snapshot: {}, backlog },
  };
}

describe("backoffDelay", () => {
  it("double le délai à chaque tentative", () => {
    expect(backoffDelay(0)).toBe(500);
    expect(backoffDelay(1)).toBe(1000);
    expect(backoffDelay(2)).toBe(2000);
    expect(backoffDelay(3)).toBe(4000);
  });

  it("plafonne le délai et tolère une tentative négative", () => {
    expect(backoffDelay(20)).toBe(30_000);
    expect(backoffDelay(-1)).toBe(500);
  });

  it("accepte une base et un plafond explicites", () => {
    expect(backoffDelay(2, { baseMs: 100, maxMs: 250 })).toBe(250);
  });
});

describe("resetOnHello", () => {
  it("remplace le log affiché par le backlog, sans concaténer", () => {
    const previous = applyTrace(emptyFeed(), entry("Ancienne"));
    const next = resetOnHello(previous, hello([entry("A"), entry("B")]));

    expect(next.entries.map((item) => item.kind)).toEqual(["A", "B"]);
    expect(next.truncated).toBe(0);
  });

  it("efface le marqueur de troncature d'une connexion précédente", () => {
    const truncated = applyTrace(
      emptyFeed(),
      entry("TraceTruncated", { payload: { dropped: 4 } }),
    );
    expect(truncated.truncated).toBe(4);

    expect(resetOnHello(truncated, hello([])).truncated).toBe(0);
  });
});

describe("applyTrace", () => {
  it("ajoute les traces vivantes dans l'ordre", () => {
    const state = applyTrace(
      applyTrace(emptyFeed(), entry("A")),
      entry("B", { level: "warning" }),
    );

    expect(state.entries.map((item) => item.kind)).toEqual(["A", "B"]);
  });

  it("conserve la borne en oubliant les plus anciennes", () => {
    const state = [entry("A"), entry("B"), entry("C")].reduce(
      (current, item) => applyTrace(current, item, 2),
      emptyFeed(),
    );

    expect(state.entries.map((item) => item.kind)).toEqual(["B", "C"]);
  });

  it("accumule un marqueur de troncature sans l'afficher comme une trace", () => {
    const marker = entry("TraceTruncated", {
      source: "console",
      level: "warning",
      payload: { dropped: 3 },
    });

    expect(isTruncation(marker)).toBe(true);
    const state = applyTrace(emptyFeed(), marker);

    expect(state.entries).toEqual([]);
    expect(state.truncated).toBe(3);
  });
});

describe("formatTimestamp", () => {
  it("affiche l'heure locale avec les millisecondes", () => {
    const seconds = 1_700_000_000.123;
    const date = new Date(seconds * 1000);
    const pad = (value: number, width = 2) =>
      String(value).padStart(width, "0");
    const expected = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
      date.getSeconds(),
    )}.${pad(date.getMilliseconds(), 3)}`;

    expect(formatTimestamp(seconds)).toBe(expected);
    expect(formatTimestamp(seconds)).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  });
});

describe("formatPayload", () => {
  it("rend le JSON replié et lisible", () => {
    expect(formatPayload({ to_state: "live_active", count: 2 })).toBe(
      '{\n  "to_state": "live_active",\n  "count": 2\n}',
    );
  });

  it("reste vide quand le payload l'est", () => {
    expect(formatPayload({})).toBe("");
  });
});

describe("connectionLabel", () => {
  it("traduit chaque état de connexion", () => {
    expect(connectionLabel("connected")).toBe("Connecté");
    expect(connectionLabel("connecting")).toBe("Connexion…");
    expect(connectionLabel("reconnecting")).toBe("Reconnexion…");
    expect(connectionLabel("disconnected")).toBe("Déconnecté");
  });
});
