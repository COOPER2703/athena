import { describe, expect, it } from "vitest";

import { originLabel, originTone } from "./origin";

describe("origine d'un tour", () => {
  it("nomme chaque origine du fil en français", () => {
    expect(originLabel("text")).toBe("Clavier");
    expect(originLabel("voice")).toBe("Voix");
    expect(originLabel("event")).toBe("Annonce");
    expect(originLabel("unknown")).toBe("Origine inconnue");
  });

  it("donne un ton distinct à la voix et aux Annonces", () => {
    expect(originTone("voice")).toBe("accent");
    expect(originTone("event")).toBe("warning");
    expect(originTone("text")).toBe("neutral");
    expect(originTone("unknown")).toBe("neutral");
  });
});
