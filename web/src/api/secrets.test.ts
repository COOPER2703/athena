import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./errors";
import { GEMINI_SECRET, saveSecret } from "./secrets";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("saveSecret", () => {
  it("écrit le Secret à son nom, en PUT, sans jamais le relire", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, {
        v: 1,
        secret: { name: GEMINI_SECRET, configured: true, updated_at: 1 },
      }),
    );

    await saveSecret(GEMINI_SECRET, "cle-gemini-de-test", fetcher);

    const [path, init] = (fetcher.mock.calls[0] ?? []) as [string, RequestInit];
    expect(path).toBe(`/api/secrets/${GEMINI_SECRET}`);
    expect(init).toMatchObject({ method: "PUT" });
    expect(JSON.parse(String(init.body))).toEqual({
      value: "cle-gemini-de-test",
    });
  });

  it("laisse une clé refusée remonter en erreur d'API", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(400, { v: 1, error: "invalid_secret" }),
    );

    const error = await saveSecret(GEMINI_SECRET, "fausse-cle", fetcher).catch(
      (reason: unknown) => reason,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 400, code: "invalid_secret" });
  });
});
