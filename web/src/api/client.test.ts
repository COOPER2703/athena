import { describe, expect, it, vi } from "vitest";

import { fetchSession, login, logout, requestJson } from "./client";
import { ApiError } from "./errors";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

type Call = [string, RequestInit];

describe("requestJson", () => {
  it("rend le corps JSON d'une réponse 2xx", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1, ok: true }),
    );
    await expect(requestJson("/api/session", {}, fetcher)).resolves.toEqual({
      v: 1,
      ok: true,
    });
  });

  it("part toujours en même origine, cookie compris", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, {}),
    );
    await requestJson("/api/status", {}, fetcher);
    const [, init] = (fetcher.mock.calls[0] ?? []) as Call;
    expect(init.credentials).toBe("same-origin");
    expect(init.headers).toMatchObject({ Accept: "application/json" });
  });

  it("convertit un refus en ApiError avec statut, code et Retry-After", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
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
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) => {
      throw new TypeError("Failed to fetch");
    });
    const error = await requestJson("/api/session", {}, fetcher).catch(
      (reason: unknown) => reason,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0, code: "network" });
  });

  it("n'exige pas de corps JSON sur une erreur", async () => {
    const fetcher = vi.fn(
      async (_path: string, _init: RequestInit) =>
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

  it("accepte un succès sans corps", async () => {
    const fetcher = vi.fn(
      async (_path: string, _init: RequestInit) =>
        ({
          ok: true,
          status: 204,
          json: async () => {
            throw new SyntaxError("vide");
          },
        }) as unknown as Response,
    );
    await expect(requestJson("/api/logout", {}, fetcher)).resolves.toBeNull();
  });
});

describe("endpoints d'identité", () => {
  it("lit et normalise /api/session", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, {
        v: 1,
        setup_required: true,
        authenticated: false,
        gemini_configured: false,
        installation_complete: false,
        restart_required: false,
        secrets_unreadable: false,
        password_min_length: 14,
      }),
    );
    await expect(fetchSession(fetcher)).resolves.toEqual({
      setupRequired: true,
      authenticated: false,
      username: null,
      geminiConfigured: false,
      installationComplete: false,
      restartRequired: false,
      secretsUnreadable: false,
      passwordMinLength: 14,
    });
  });

  it("envoie les identifiants en JSON et rend l'identité connectée", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1, authenticated: true, username: "admin" }),
    );
    await expect(
      login({ username: "admin", password: "secret" }, fetcher),
    ).resolves.toEqual({
      setupRequired: false,
      authenticated: true,
      username: "admin",
      geminiConfigured: false,
      installationComplete: false,
      restartRequired: false,
      secretsUnreadable: false,
      passwordMinLength: null,
    });
    const [path, init] = (fetcher.mock.calls[0] ?? []) as Call;
    expect(path).toBe("/api/login");
    expect(init).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(init.body))).toEqual({
      username: "admin",
      password: "secret",
    });
  });

  it("déconnecte par POST /api/logout", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1, authenticated: false }),
    );
    await logout(fetcher);
    const [path, init] = (fetcher.mock.calls[0] ?? []) as Call;
    expect(path).toBe("/api/logout");
    expect(init.method).toBe("POST");
  });
});
