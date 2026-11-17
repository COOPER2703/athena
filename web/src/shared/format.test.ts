import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatDate,
  formatDuration,
  formatIsoDate,
  formatTimestamp,
  pluralize,
  shortId,
} from "./format";

describe("horodatages", () => {
  it("affiche l'heure avec les secondes", () => {
    expect(formatTimestamp(1_700_000_000, "UTC")).toBe("22:13:20");
  });

  it("affiche une date courte, ou un tiret si elle manque", () => {
    expect(formatDate(1_700_000_000, "UTC")).toBe("14/11/2023 22:13");
    expect(formatDate(null, "UTC")).toBe("—");
    expect(formatDate(Number.NaN, "UTC")).toBe("—");
  });
});

describe("durées", () => {
  it("choisit l'unité lisible", () => {
    expect(formatDuration(0)).toBe("0 s");
    expect(formatDuration(45)).toBe("45 s");
    expect(formatDuration(180)).toBe("3 min");
    expect(formatDuration(3 * 3600 + 5 * 60)).toBe("3 h 05 min");
    expect(formatDuration(2 * 86400 + 4 * 3600)).toBe("2 j 4 h");
  });

  it("rend un tiret pour une durée absente", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(-1)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
  });
});

describe("tailles et identifiants", () => {
  it("affiche des octets en unités françaises", () => {
    expect(formatBytes(0)).toBe("0 o");
    expect(formatBytes(512)).toBe("512 o");
    expect(formatBytes(1024)).toBe("1 Ko");
    expect(formatBytes(1536)).toBe("1,5 Ko");
    expect(formatBytes(1_500_000)).toBe("1,4 Mo");
  });

  it("raccourcit un identifiant trop long", () => {
    expect(shortId("abcd1234")).toBe("abcd1234");
    expect(shortId("abcd12345678")).toBe("abcd1234…");
    expect(shortId("")).toBe("—");
  });
});

describe("pluralize", () => {
  it("accorde le nom avec le nombre", () => {
    expect(pluralize(0, "Trace")).toBe("0 Trace");
    expect(pluralize(1, "Trace")).toBe("1 Trace");
    expect(pluralize(2, "Trace")).toBe("2 Traces");
    expect(pluralize(2, "Jeton", "Jetons")).toBe("2 Jetons");
  });
});

describe("horodatage ISO du fil", () => {
  it("met en forme une date ISO, ou un tiret si elle est illisible", () => {
    expect(formatIsoDate("2026-11-17T10:00:00Z", "UTC")).toBe(
      "17/11/2026 10:00",
    );
    expect(formatIsoDate("pas une date", "UTC")).toBe("—");
    expect(formatIsoDate("", "UTC")).toBe("—");
  });
});
