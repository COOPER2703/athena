import { describe, expect, it } from "vitest";

import { formatTokenDate } from "./format";
import {
  formatLastUsed,
  normalizeIssuedToken,
  normalizeTokens,
  tokenStatusLabel,
  tokenTargetLabel,
  validateTokenDraft,
  type AccessToken,
} from "./tokens";

const TOKEN: AccessToken = {
  id: "t1",
  name: "Portable",
  kind: "client",
  source: null,
  prefix: "ath_abcd1234",
  createdAt: 1_700_000_000,
  lastUsedAt: null,
  revokedAt: null,
};

describe("normalizeTokens", () => {
  it("rend une liste vide pour une réponse vide ou mal typée", () => {
    expect(normalizeTokens(null)).toEqual([]);
    expect(normalizeTokens({})).toEqual([]);
    expect(normalizeTokens({ tokens: "nope" })).toEqual([]);
  });

  it("lit les champs du contrat et écarte les entrées inutilisables", () => {
    expect(
      normalizeTokens({
        tokens: [
          {
            id: "t1",
            name: "Portable",
            kind: "client",
            source: null,
            prefix: "ath_abcd1234",
            created_at: 1_700_000_000,
            last_used_at: 1_700_000_060,
            revoked_at: null,
          },
          {
            id: "t2",
            name: "Esp32",
            kind: "source",
            source: "matrix",
            prefix: "ath_efgh5678",
            created_at: 1_700_000_100,
            last_used_at: null,
            revoked_at: 1_700_000_200,
          },
          { id: "sans-nom" },
          { name: "sans-identifiant", kind: "client" },
          "pas un objet",
        ],
      }),
    ).toEqual([
      { ...TOKEN, lastUsedAt: 1_700_000_060 },
      {
        id: "t2",
        name: "Esp32",
        kind: "source",
        source: "matrix",
        prefix: "ath_efgh5678",
        createdAt: 1_700_000_100,
        lastUsedAt: null,
        revokedAt: 1_700_000_200,
      },
    ]);
  });

  it("ne recopie jamais un secret présent dans une entrée de liste", () => {
    const tokens = normalizeTokens({
      tokens: [
        {
          id: "t1",
          name: "Portable",
          kind: "client",
          source: null,
          prefix: "ath_abcd1234",
          created_at: 1_700_000_000,
          last_used_at: null,
          revoked_at: null,
          secret: "ath_fuite",
          secret_hash: "empreinte",
        },
      ],
    });
    expect(tokens).toHaveLength(1);
    const token = tokens[0];
    expect(token).toBeDefined();
    if (token === undefined) {
      return;
    }

    expect(Object.keys(token).sort()).toEqual([
      "createdAt",
      "id",
      "kind",
      "lastUsedAt",
      "name",
      "prefix",
      "revokedAt",
      "source",
    ]);
  });
});

describe("normalizeIssuedToken", () => {
  it("extrait le secret rendu une seule fois", () => {
    expect(
      normalizeIssuedToken({
        token: {
          id: "t1",
          name: "Portable",
          kind: "client",
          source: null,
          prefix: "ath_abcd1234",
          created_at: 1_700_000_000,
          last_used_at: null,
          revoked_at: null,
        },
        secret: "ath_secret-complet",
      }),
    ).toEqual({ token: TOKEN, secret: "ath_secret-complet" });
  });

  it("refuse une création sans secret ou sans Jeton lisible", () => {
    expect(normalizeIssuedToken({ token: TOKEN })).toBeNull();
    expect(normalizeIssuedToken({ token: TOKEN, secret: "   " })).toBeNull();
    expect(normalizeIssuedToken({ secret: "ath_x" })).toBeNull();
    expect(normalizeIssuedToken(null)).toBeNull();
  });
});

describe("labels", () => {
  it("nomme la nature ou la source d'un Jeton", () => {
    expect(tokenTargetLabel(TOKEN)).toBe("Transport client");
    expect(
      tokenTargetLabel({ ...TOKEN, kind: "source", source: "matrix" }),
    ).toBe("Source « matrix »");
    expect(tokenTargetLabel({ ...TOKEN, kind: "source", source: null })).toBe(
      "Source",
    );
  });

  it("distingue un Jeton actif d'un Jeton révoqué", () => {
    expect(tokenStatusLabel(TOKEN)).toBe("Actif");
    expect(tokenStatusLabel({ ...TOKEN, revokedAt: 1_700_000_200 })).toBe(
      "Révoqué",
    );
  });
});

describe("dates", () => {
  it("affiche un tiret quand la date manque ou est illisible", () => {
    expect(formatTokenDate(null)).toBe("—");
    expect(formatTokenDate(Number.NaN)).toBe("—");
    expect(formatLastUsed(null)).toBe("Jamais utilisé");
  });

  it("formate une date lisible, sans « Invalid Date »", () => {
    const label = formatTokenDate(1_700_000_000);
    expect(label).toMatch(/^\d{2}\/\d{2}\/\d{4} \d{2}:\d{2}$/);
    expect(formatLastUsed(1_700_000_000)).toBe(label);
  });
});

describe("validateTokenDraft", () => {
  it("nettoie une saisie client valide et ignore une source parasite", () => {
    expect(
      validateTokenDraft({ name: "  Portable  ", kind: "client", source: "x" }),
    ).toEqual({
      ok: true,
      value: { name: "Portable", kind: "client", source: null },
    });
  });

  it("exige une source pour un Jeton source", () => {
    expect(
      validateTokenDraft({ name: "Esp32", kind: "source", source: "   " }),
    ).toEqual({
      ok: false,
      message: "Une source est obligatoire pour un Jeton source.",
    });
    expect(
      validateTokenDraft({ name: "Esp32", kind: "source", source: " matrix " }),
    ).toEqual({
      ok: true,
      value: { name: "Esp32", kind: "source", source: "matrix" },
    });
  });

  it("refuse un nom vide", () => {
    expect(
      validateTokenDraft({ name: "   ", kind: "client", source: "" }),
    ).toEqual({
      ok: false,
      message: "Le nom du Jeton ne peut pas être vide.",
    });
  });
});
