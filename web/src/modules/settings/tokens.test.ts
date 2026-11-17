import { describe, expect, it } from "vitest";

import type { AccessToken } from "../../api/tokens";
import {
  SECRET_ONCE_WARNING,
  clientTransportUrl,
  couplingCommand,
  tokenStatusLabel,
  tokenTargetLabel,
  validateTokenDraft,
} from "./tokens";

const TOKEN: AccessToken = {
  id: "tok-1",
  name: "Portable",
  kind: "client",
  source: null,
  prefix: "ath_abc",
  createdAt: 1_760_000_000,
  lastUsedAt: null,
  revokedAt: null,
};

describe("libellés des Jetons", () => {
  it("distingue le transport d'une source", () => {
    expect(tokenTargetLabel(TOKEN)).toBe("Transport client");
    expect(
      tokenTargetLabel({ ...TOKEN, kind: "source", source: "matrix" }),
    ).toBe("Source « matrix »");
    expect(tokenTargetLabel({ ...TOKEN, kind: "source", source: null })).toBe(
      "Source",
    );
  });

  it("garde la révocation visible après coup", () => {
    expect(tokenStatusLabel(TOKEN)).toBe("Actif");
    expect(tokenStatusLabel({ ...TOKEN, revokedAt: 1_760_000_100 })).toBe(
      "Révoqué",
    );
  });

  it("rappelle que le secret ne sera plus jamais affiché", () => {
    expect(SECRET_ONCE_WARNING).toContain("ne sera plus jamais affiché");
  });
});

describe("validation de la création", () => {
  it("exige un nom", () => {
    expect(
      validateTokenDraft({ name: "  ", kind: "client", source: "" }),
    ).toEqual({ ok: false, message: "Le nom du Jeton ne peut pas être vide." });
  });

  it("exige une source pour un Jeton source", () => {
    expect(
      validateTokenDraft({ name: "Matrix", kind: "source", source: " " }),
    ).toEqual({
      ok: false,
      message: "Une source est obligatoire pour un Jeton source.",
    });
  });

  it("élague la saisie et rend la charge utile attendue", () => {
    expect(
      validateTokenDraft({ name: " Portable ", kind: "client", source: "x" }),
    ).toEqual({
      ok: true,
      value: { name: "Portable", kind: "client", source: null },
    });
    expect(
      validateTokenDraft({ name: "Matrix", kind: "source", source: " matrix " }),
    ).toEqual({
      ok: true,
      value: { name: "Matrix", kind: "source", source: "matrix" },
    });
  });
});

describe("commande de couplage", () => {
  it("compose la commande du Client desktop avec l'URL et le Jeton", () => {
    expect(
      couplingCommand("ws://127.0.0.1:8765", "ath_secret"),
    ).toBe(
      'WS_SERVER_URL="ws://127.0.0.1:8765" ATHENA_ACCESS_TOKEN="ath_secret" python -m client',
    );
  });

  it("remplace un hôte d'écoute par l'hôte réellement joint", () => {
    expect(
      clientTransportUrl("ws://0.0.0.0:8765", "192.168.1.20"),
    ).toBe("ws://192.168.1.20:8765");
    expect(clientTransportUrl("ws://[::]:8765", "athena.local")).toBe(
      "ws://athena.local:8765",
    );
    expect(clientTransportUrl("ws://127.0.0.1:8765", "athena.local")).toBe(
      "ws://127.0.0.1:8765",
    );
  });

  it("garde le schéma et le port, ou renonce à une URL illisible", () => {
    expect(clientTransportUrl("wss://athena.test:8765", "athena.test")).toBe(
      "wss://athena.test:8765",
    );
    expect(clientTransportUrl("http://athena.test:8765", "athena.test")).toBeNull();
    expect(clientTransportUrl("pas une url", "athena.test")).toBeNull();
    expect(clientTransportUrl(null, "athena.test")).toBeNull();
  });
});
