import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./errors";
import { MAX_MESSAGE_CHARS, openTour } from "./chat";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

type Call = [string, RequestInit];

describe("openTour", () => {
  it("poste le message et demande un flux", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, null),
    );

    await openTour("Bonjour Athena", new AbortController().signal, fetcher);

    const [path, init] = (fetcher.mock.calls[0] ?? []) as Call;
    expect(path).toBe("/api/chat");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      body: JSON.stringify({ text: "Bonjour Athena" }),
    });
    expect(init.headers).toMatchObject({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    });
  });

  it("rend la réponse d'un flux accepté", async () => {
    const accepted = response(200, null);
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) => accepted);

    await expect(
      openTour("Bonjour", new AbortController().signal, fetcher),
    ).resolves.toBe(accepted);
  });

  it("traduit un refus d'Installation en erreur à champ", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(409, {
        v: 1,
        error: "installation_incomplete",
        message: "Clé manquante.",
        fields: { GEMINI_API_KEY: "required" },
      }),
    );

    const error = await openTour(
      "Bonjour",
      new AbortController().signal,
      fetcher,
    ).catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "installation_incomplete",
    });
  });

  it("convertit une panne réseau en ApiError sans perdre l'annulation", async () => {
    const offline = vi.fn(async (_path: string, _init: RequestInit) => {
      throw new TypeError("Failed to fetch");
    });
    const failure = await openTour(
      "Bonjour",
      new AbortController().signal,
      offline,
    ).catch((reason: unknown) => reason);
    expect(failure).toMatchObject({ status: 0, code: "network" });

    const controller = new AbortController();
    controller.abort();
    const aborted = vi.fn(async (_path: string, _init: RequestInit) => {
      throw new DOMException("annulé", "AbortError");
    });
    const reason = await openTour("Bonjour", controller.signal, aborted).catch(
      (error: unknown) => error,
    );
    expect(reason).not.toBeInstanceOf(ApiError);
    expect((reason as { name?: string }).name).toBe("AbortError");
  });

  it("borne le message au plafond du serveur", () => {
    expect(MAX_MESSAGE_CHARS).toBe(4096);
  });
});
