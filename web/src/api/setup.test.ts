import { describe, expect, it, vi } from "vitest";

import { setupAccount } from "./setup";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("setupAccount", () => {
  it("crée le Compte par POST /api/setup et rend la clé maître une fois", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, {
        v: 1,
        authenticated: true,
        username: "admin",
        master_key: "cle-maitre",
      }),
    );

    const { session, masterKey } = await setupAccount(
      { username: "admin", password: "mot-de-passe-solide" },
      fetcher,
    );

    expect(session).toMatchObject({
      setupRequired: false,
      authenticated: true,
      username: "admin",
    });
    expect(masterKey).toBe("cle-maitre");

    const [path, init] = (fetcher.mock.calls[0] ?? []) as [string, RequestInit];
    expect(path).toBe("/api/setup");
    expect(init).toMatchObject({ method: "POST" });
    expect(JSON.parse(String(init.body))).toEqual({
      username: "admin",
      password: "mot-de-passe-solide",
    });
  });
});
