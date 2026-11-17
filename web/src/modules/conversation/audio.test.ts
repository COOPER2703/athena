import { describe, expect, it } from "vitest";

import { decodeBase64, pcm16ToFloat32 } from "./audio";

/** Deux octets little-endian d'un échantillon signé 16 bits. */
function sample(value: number): Uint8Array {
  const clamped = Math.max(-32_768, Math.min(32_767, value));
  const unsigned = clamped < 0 ? clamped + 65_536 : clamped;
  return Uint8Array.of(unsigned & 0xff, (unsigned >> 8) & 0xff);
}

describe("pcm16ToFloat32", () => {
  it("ramène les échantillons signés 16 bits entre -1 et 1", () => {
    expect([...pcm16ToFloat32(sample(0))]).toEqual([0]);
    expect([...pcm16ToFloat32(sample(16_384))]).toEqual([0.5]);
    expect([...pcm16ToFloat32(sample(-32_768))]).toEqual([-1]);
    expect(pcm16ToFloat32(sample(32_767))[0]).toBeCloseTo(1, 4);
  });

  it("lit les octets en little-endian, comme le serveur les envoie", () => {
    // 0x4000 = 16384 en little-endian : l'octet fort vient en second.
    expect([...pcm16ToFloat32(Uint8Array.of(0x00, 0x40))]).toEqual([0.5]);
  });

  it("ignore un octet final orphelin plutôt que de lire hors trame", () => {
    const bytes = Uint8Array.of(0x00, 0x40, 0x7f);
    expect([...pcm16ToFloat32(bytes)]).toEqual([0.5]);
  });

  it("rend un tableau vide pour une entrée vide", () => {
    expect([...pcm16ToFloat32(new Uint8Array())]).toEqual([]);
  });
});

describe("decodeBase64", () => {
  it("décode une charge audio en octets", () => {
    // Quatre octets nuls : deux échantillons de silence.
    expect([...decodeBase64("AAAAAA==")]).toEqual([0, 0, 0, 0]);
  });

  it("rend un tableau vide pour une charge illisible", () => {
    expect([...decodeBase64("pas du base64 !")]).toEqual([]);
    expect([...decodeBase64("")]).toEqual([]);
  });
});
