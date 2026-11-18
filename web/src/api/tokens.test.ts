import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./errors";
import {
  createAccessToken,
  fetchTokens,
  normalizeIssuedToken,
  normalizeTokens,
  revokeAccessToken,
} from "./tokens";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const TOKEN = {
  id: "tok-1",
  name: "Portable",
  kind: "client",
  source: null,
  prefix: "ath_abc",
  created_at: 1_760_000_000,
  last_used_at: null,
  revoked_at: null,
};

describe("normalizeTokens", () => {
  it("lit la liste sans jamais copier de secret", () => {
    const tokens = normalizeTokens({
      v: 1,
      tokens: [{ ...TOKEN, secret: "ath_secret_interdit" }],
    });

    expect(tokens).toEqual([
      {
        id: "tok-1",
        name: "Portable",
        kind: "client",
        source: null,
        prefix: "ath_abc",
        createdAt: 1_760_000_000,
        lastUsedAt: null,
        revokedAt: null,
      },
    ]);
    expect(JSON.stringify(tokens)).not.toContain("ath_secret_interdit");
  });

  it("ignore une entrée sans identifiant ou de nature inconnue", () => {
    expect(
      normalizeTokens({
        tokens: [
          { ...TOKEN, id: "" },
          { ...TOKEN, name: "" },
          { ...TOKEN, kind: "futur" },
          { ...TOKEN, id: "tok-2" },
        ],
      }),
    ).toHaveLength(1);
  });

  it("rend une liste vide pour un corps illisible", () => {
    expect(normalizeTokens(null)).toEqual([]);
    expect(normalizeTokens({ tokens: "aucun" })).toEqual([]);
  });

  it("normalise une source seulement pour un Jeton de nature source", () => {
    const [client, source] = normalizeTokens({
      tokens: [
        { ...TOKEN, source: "matrix" },
        { ...TOKEN, id: "tok-2", kind: "source", source: "matrix" },
      ],
    });

    expect(client?.source).toBeNull();
    expect(source?.source).toBe("matrix");
  });
});

describe("normalizeIssuedToken", () => {
  it("exige le secret, rendu une seule fois", () => {
    expect(
      normalizeIssuedToken({ v: 1, token: TOKEN, secret: "ath_secret" }),
    ).toEqual({
      token: expect.objectContaining({ id: "tok-1" }),
      secret: "ath_secret",
    });
    expect(normalizeIssuedToken({ v: 1, token: TOKEN })).toBeNull();
  });
});

describe("routes des Jetons", () => {
  it("liste les Jetons sur leur route dédiée", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1, tokens: [TOKEN] }),
    );

    const tokens = await fetchTokens(fetcher);

    expect(tokens).toHaveLength(1);
    const [path] = (fetcher.mock.calls[0] ?? []) as [string, RequestInit];
    expect(path).toBe("/api/tokens");
  });

  it("crée un Jeton en envoyant sa saisie validée", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(201, { v: 1, token: TOKEN, secret: "ath_secret" }),
    );

    const issued = await createAccessToken(
      { name: "Portable", kind: "client", source: null },
      fetcher,
    );

    const [path, init] = (fetcher.mock.calls[0] ?? []) as [string, RequestInit];
    expect(path).toBe("/api/tokens");
    expect(init).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(init.body))).toEqual({
      name: "Portable",
      kind: "client",
      source: null,
    });
    expect(issued.secret).toBe("ath_secret");
  });

  it("signale une réponse de création sans secret", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(201, { v: 1, token: TOKEN }),
    );

    const error = await createAccessToken(
      { name: "Portable", kind: "client", source: null },
      fetcher,
    ).catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: "invalid_response" });
  });

  it("révoque un Jeton par son identifiant", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1 }),
    );

    await revokeAccessToken("tok/1", fetcher);

    const [path, init] = (fetcher.mock.calls[0] ?? []) as [string, RequestInit];
    expect(path).toBe("/api/tokens/tok%2F1");
    expect(init).toMatchObject({ method: "DELETE" });
  });
});
