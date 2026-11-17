import { describe, expect, it } from "vitest";

import { createSseParser, type SseEvent } from "./sse";

function parseAll(chunks: readonly string[]): SseEvent[] {
  const parser = createSseParser();
  const events: SseEvent[] = [];
  for (const chunk of chunks) {
    events.push(...parser.push(chunk));
  }
  return [...events, ...parser.flush()];
}

describe("analyseur SSE", () => {
  it("lit un événement simple", () => {
    expect(parseAll(["event: text\ndata: Bonjour\n\n"])).toEqual([
      { event: "text", data: "Bonjour" },
    ]);
  });

  it("assemble les données sur plusieurs lignes", () => {
    expect(parseAll(["event: text\ndata: ligne 1\ndata: ligne 2\n\n"])).toEqual([
      { event: "text", data: "ligne 1\nligne 2" },
    ]);
  });

  it("accepte des trames coupées entre deux morceaux", () => {
    expect(parseAll(["event: te", "xt\ndata: Bon", "jour\n", "\n"])).toEqual([
      { event: "text", data: "Bonjour" },
    ]);
  });

  it("gère les fins de ligne CRLF et CR", () => {
    expect(parseAll(["event: done\r\ndata: fin\r\n\r\n"])).toEqual([
      { event: "done", data: "fin" },
    ]);
    expect(parseAll(["event: done\rdata: fin\r\r"])).toEqual([
      { event: "done", data: "fin" },
    ]);
  });

  it("retient l'identifiant et le délai de reconnexion", () => {
    expect(parseAll(["id: 7\nretry: 1500\ndata: x\n\n"])).toEqual([
      { event: "message", data: "x", id: "7", retry: 1500 },
    ]);
  });

  it("ignore les commentaires et les champs inconnus", () => {
    expect(
      parseAll([": battement\nfoo: bar\nevent: busy\ndata: occupé\n\n"]),
    ).toEqual([{ event: "busy", data: "occupé" }]);
  });

  it("n'émet rien pour un bloc sans donnée", () => {
    expect(parseAll(["event: text\n\n"])).toEqual([]);
    expect(parseAll(["\n\n"])).toEqual([]);
  });

  it("ne retire qu'un espace après les deux-points", () => {
    expect(parseAll(["data:  deux espaces\n\n"])).toEqual([
      { event: "message", data: " deux espaces" },
    ]);
  });

  it("ignore un délai de reconnexion qui n'est pas un entier", () => {
    expect(parseAll(["retry: bientôt\ndata: x\n\n"])).toEqual([
      { event: "message", data: "x" },
    ]);
  });

  it("finit un événement incomplet au flush", () => {
    expect(parseAll(["event: error\ndata: échec"])).toEqual([
      { event: "error", data: "échec" },
    ]);
  });

  it("tolère un BOM en tête de flux", () => {
    expect(parseAll(["\uFEFFdata: x\n\n"])).toEqual([
      { event: "message", data: "x" },
    ]);
  });
});
