import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  createToken,
  fetchSession,
  listTokens,
  login,
  requestJson,
  revokeToken,
  setupAccount,
} from "./api";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("requestJson", () => {
  it("rend le corps JSON d'une réponse 2xx", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) => response(200, { v: 1, ok: true }));
    await expect(requestJson("/api/session", {}, fetcher)).resolves.toEqual({
      v: 1,
      ok: true,
    });
  });

  it("convertit un refus en ApiError avec statut, code et Retry-After", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(429, {
        v: 1,
        error: "too_many_attempts",
        message: "Trop de tentatives.",
        retry_after: 900,
      }),
    );
    const error = await requestJson("/api/login", {}, fetcher).catch(
      (reason: unknown) => reason,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 429,
      code: "too_many_attempts",
      retryAfter: 900,
    });
  });

  it("convertit une panne réseau en ApiError de statut 0", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) => {
      throw new TypeError("Failed to fetch");
    });
    const error = await requestJson("/api/session", {}, fetcher).catch(
      (reason: unknown) => reason,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0, code: "network" });
  });

  it("n'exige jamais un corps JSON sur une erreur", async () => {
    const fetcher = vi.fn(
      async (_input: string, _init: RequestInit) =>
        ({
          ok: false,
          status: 502,
          json: async () => {
            throw new SyntaxError("pas du JSON");
          },
        }) as unknown as Response,
    );
    const error = await requestJson("/api/status", {}, fetcher).catch(
      (reason: unknown) => reason,
    );
    expect(error).toMatchObject({ status: 502, code: "http_error" });
  });
});

describe("session et formulaires", () => {
  it("normalise la réponse de session", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(200, { setup_required: true, authenticated: false }),
    );
    await expect(fetchSession(undefined, fetcher)).resolves.toEqual({
      setupRequired: true,
      authenticated: false,
      username: null,
    });
  });

  it("envoie les identifiants en JSON et rend l'identité connectée", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(200, { authenticated: true, username: "admin" }),
    );
    await expect(
      login({ username: "admin", password: "secret" }, fetcher),
    ).resolves.toEqual({
      setupRequired: false,
      authenticated: true,
      username: "admin",
    });
    const [path, init] = fetcher.mock.calls[0] ?? [];
    expect(path).toBe("/api/login");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
    });
    expect(JSON.parse(String(init?.body))).toEqual({
      username: "admin",
      password: "secret",
    });
  });

  it("envoie le Code d'installation à /api/setup", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(200, { authenticated: true, username: "admin" }),
    );
    await setupAccount(
      { code: "abc", username: "admin", password: "secret" },
      fetcher,
    );
    const [path, init] = fetcher.mock.calls[0] ?? [];
    expect(path).toBe("/api/setup");
    expect(JSON.parse(String(init?.body))).toEqual({
      code: "abc",
      username: "admin",
      password: "secret",
    });
  });
});

const LISTED_TOKEN = {
  id: "t1",
  name: "Portable",
  kind: "client",
  source: null,
  prefix: "ath_abcd1234",
  created_at: 1_700_000_000,
  last_used_at: null,
  revoked_at: null,
};

describe("jetons d'accès", () => {
  it("liste les Jetons via GET /api/tokens", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(200, { v: 1, tokens: [LISTED_TOKEN] }),
    );

    await expect(listTokens(fetcher)).resolves.toEqual([
      {
        id: "t1",
        name: "Portable",
        kind: "client",
        source: null,
        prefix: "ath_abcd1234",
        createdAt: 1_700_000_000,
        lastUsedAt: null,
        revokedAt: null,
      },
    ]);
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/tokens");
  });

  it("crée un Jeton et rend le secret une seule fois", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(201, { v: 1, token: LISTED_TOKEN, secret: "ath_secret" }),
    );

    await expect(
      createToken({ name: "Portable", kind: "client", source: null }, fetcher),
    ).resolves.toMatchObject({ secret: "ath_secret" });
    const [path, init] = fetcher.mock.calls[0] ?? [];
    expect(path).toBe("/api/tokens");
    expect(init).toMatchObject({ method: "POST", credentials: "same-origin" });
    expect(JSON.parse(String(init?.body))).toEqual({
      name: "Portable",
      kind: "client",
      source: null,
    });
  });

  it("révoque un Jeton par son identifiant, sans secret dans l'URL", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(200, {
        v: 1,
        token: { ...LISTED_TOKEN, revoked_at: 1_700_000_200 },
      }),
    );

    await revokeToken("t/1", fetcher);

    const [path, init] = fetcher.mock.calls[0] ?? [];
    expect(path).toBe("/api/tokens/t%2F1");
    expect(init).toMatchObject({ method: "DELETE" });
  });
});
