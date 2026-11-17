import { describe, expect, it } from "vitest";

import {
  DEFAULT_BACKOFF,
  MAX_FEED_ENTRIES,
  TRUNCATION_KIND,
  applyTrace,
  backoffDelay,
  connectionLabel,
  connectionTone,
  emptyFeed,
  formatTraceLevel,
  formatTracePayload,
  formatTraceTime,
  isTraceLevel,
  parseStreamFrame,
  resetOnHello,
  traceLevelTone,
  type HelloFrame,
  type TraceEntry,
} from "./stream";

function entry(overrides: Partial<TraceEntry> = {}): TraceEntry {
  return {
    timestamp: 1_700_000_000.5,
    source: "gemini",
    kind: "LlmFailed",
    level: "error",
    payload: { cause: "x" },
    ...overrides,
  };
}

function hello(backlog: TraceEntry[]): HelloFrame {
  return {
    v: 1,
    type: "hello",
    data: { snapshot: { session: {} }, backlog },
  };
}

describe("niveaux de Trace", () => {
  it("reconnaît les cinq niveaux du flux", () => {
    for (const level of ["debug", "info", "warning", "error", "critical"]) {
      expect(isTraceLevel(level)).toBe(true);
    }
    expect(isTraceLevel("fatal")).toBe(false);
  });

  it("traduit le niveau et lui donne une tonalité bornée", () => {
    expect(formatTraceLevel("warning")).toBe("Avertissement");
    expect(formatTraceLevel("critique")).toBe("critique");
    expect(formatTraceLevel("")).toBe("Inconnu");
    expect(traceLevelTone("critical")).toBe("danger");
    expect(traceLevelTone("error")).toBe("danger");
    expect(traceLevelTone("warning")).toBe("warning");
    expect(traceLevelTone("info")).toBe("neutral");
    expect(traceLevelTone("futur")).toBe("neutral");
  });
});

describe("formatage d'une Trace", () => {
  it("horodate avec les millisecondes et retombe sur un tiret", () => {
    const rendered = formatTraceTime(1_700_000_000.5);
    expect(rendered).toMatch(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
    expect(formatTraceTime(Number.NaN)).toBe("—");
  });

  it("rend un payload vide comme une chaîne vide", () => {
    expect(formatTracePayload({})).toBe("");
    expect(formatTracePayload({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
});

describe("trames du flux", () => {
  it("accepte une trame hello complète et un backlog typé", () => {
    const frame = parseStreamFrame(hello([entry()]));
    expect(frame?.type).toBe("hello");
    expect(parseStreamFrame(hello([]))).not.toBeNull();
  });

  it("refuse une trame inconnue ou mal formée", () => {
    expect(parseStreamFrame({ type: "futur", data: {} })).toBeNull();
    expect(
      parseStreamFrame({ type: "hello", data: { snapshot: {}, backlog: [{}] } }),
    ).toBeNull();
    expect(parseStreamFrame({ type: "trace", data: { source: "x" } })).toBeNull();
    expect(parseStreamFrame(null)).toBeNull();
  });

  it("accepte une trame status et une trame trace valides", () => {
    expect(parseStreamFrame({ type: "status", data: { session: {} } })?.type).toBe(
      "status",
    );
    expect(parseStreamFrame({ type: "trace", data: entry() })?.type).toBe(
      "trace",
    );
  });
});

describe("feed borné", () => {
  it("remplace le backlog au hello et incrémente la génération", () => {
    const state = resetOnHello(emptyFeed(), hello([entry(), entry()]));
    expect(state.entries).toHaveLength(2);
    expect(state.generation).toBe(1);
    expect(state.truncated).toBe(0);
    expect(state.browserDropped).toBe(0);
  });

  it("tronque un backlog plus grand que la borne du navigateur", () => {
    const backlog = Array.from({ length: MAX_FEED_ENTRIES + 2 }, () =>
      entry(),
    );
    const state = resetOnHello(emptyFeed(), hello(backlog));
    expect(state.entries).toHaveLength(MAX_FEED_ENTRIES);
    expect(state.browserDropped).toBe(2);
  });

  it("ajoute une trace vivante et compte les oublis de la borne", () => {
    let state = emptyFeed();
    state = applyTrace(state, entry(), 2);
    state = applyTrace(state, entry({ kind: "a" }), 2);
    state = applyTrace(state, entry({ kind: "b" }), 2);

    expect(state.entries.map((item) => item.kind)).toEqual(["a", "b"]);
    expect(state.browserDropped).toBe(1);
  });

  it("compte le marqueur de troncature sans l'afficher", () => {
    const state = applyTrace(
      emptyFeed(),
      entry({ kind: TRUNCATION_KIND, payload: { dropped: 7 } }),
    );
    expect(state.entries).toEqual([]);
    expect(state.truncated).toBe(7);
  });
});

describe("connexion au flux", () => {
  it("libelle et colore les états connus", () => {
    expect(connectionLabel("connected")).toBe("Flux connecté");
    expect(connectionLabel("reconnecting")).toBe("Reconnexion…");
    expect(connectionTone("connected")).toBe("accent");
    expect(connectionTone("disconnected")).toBe("warning");
    expect(connectionTone("connecting")).toBe("neutral");
  });

  it("double le délai de reconnexion jusqu'au plafond", () => {
    expect(backoffDelay(0)).toBe(0);
    expect(backoffDelay(1)).toBe(DEFAULT_BACKOFF.baseMs);
    expect(backoffDelay(2)).toBe(DEFAULT_BACKOFF.baseMs * 2);
    expect(backoffDelay(100)).toBe(DEFAULT_BACKOFF.maxMs);
  });
});
