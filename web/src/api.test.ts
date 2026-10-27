import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  changePassword,
  fetchSession,
  login,
  logout,
  logoutAll,
  requestJson,
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

  it("change le mot de passe avec les deux champs du contrat", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(200, { v: 1, changed: true }),
    );
    await changePassword(
      { current_password: "ancien", new_password: "nouveau" },
      fetcher,
    );
    const [path, init] = fetcher.mock.calls[0] ?? [];
    expect(path).toBe("/api/password");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
    });
    expect(JSON.parse(String(init?.body))).toEqual({
      current_password: "ancien",
      new_password: "nouveau",
    });
  });

  it("déconnecte le navigateur courant et tous les autres", async () => {
    const fetcher = vi.fn(async (_input: string, _init: RequestInit) =>
      response(200, { v: 1, authenticated: false }),
    );
    await logout(fetcher);
    await logoutAll(fetcher);
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual([
      "/api/logout",
      "/api/logout-all",
    ]);
    for (const [, init] of fetcher.mock.calls) {
      expect(init).toMatchObject({
        method: "POST",
        credentials: "same-origin",
      });
    }
  });
});
