import { describe, expect, it, vi } from "vitest";

import {
  ACCEPTED_MESSAGE,
  DEFAULT_SOURCE,
  MAX_CONTENT_CHARS,
  characterCount,
  eventErrorMessage,
  submitDraft,
  validateEventDraft,
} from "./events";

describe("validateEventDraft", () => {
  it("accepte une saisie valide et la nettoie", () => {
    expect(
      validateEventDraft({ source: "  console  ", content: "  porte ouverte  " }),
    ).toEqual({ ok: true, source: "console", content: "porte ouverte" });
  });

  it("refuse une Source ou un Contenu vide après trim", () => {
    expect(validateEventDraft({ source: "   ", content: "porte ouverte" })).toEqual({
      ok: false,
      message: "La Source ne peut pas être vide.",
    });
    expect(validateEventDraft({ source: "console", content: " \n " })).toEqual({
      ok: false,
      message: "Le contenu de l'Événement ne peut pas être vide.",
    });
  });

  it("accepte exactement le plafond et refuse au-delà", () => {
    expect(
      validateEventDraft({
        source: "console",
        content: "x".repeat(MAX_CONTENT_CHARS),
      }).ok,
    ).toBe(true);

    const tooLong = validateEventDraft({
      source: "console",
      content: "x".repeat(MAX_CONTENT_CHARS + 1),
    });
    expect(tooLong.ok).toBe(false);
    if (!tooLong.ok) {
      expect(tooLong.message).toContain("4 096");
    }
  });
});

describe("characterCount", () => {
  it("compte la saisie utile, sans les espaces de bord", () => {
    expect(characterCount("  porte ouverte  ")).toBe(13);
    expect(characterCount("")).toBe(0);
  });
});

describe("eventErrorMessage", () => {
  it("préfère le message de l'API", () => {
    expect(
      eventErrorMessage(400, { error: "source and content must be non-empty" }),
    ).toBe("source and content must be non-empty");
  });

  it("retombe sur un message lisible sinon", () => {
    expect(eventErrorMessage(500, null)).toBe(
      "Le serveur a refusé l'Événement (500).",
    );
    expect(eventErrorMessage(503, { error: "  " })).toBe(
      "Le serveur a refusé l'Événement (503).",
    );
  });
});

describe("submitDraft", () => {
  it("n'émet aucune requête quand la saisie est refusée", async () => {
    const fetcher = vi.fn();

    const result = await submitDraft({ source: "console", content: "   " }, fetcher);

    expect(result.ok).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("envoie la saisie nettoyée et rend l'accusé d'acceptation", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 202 }));

    const result = await submitDraft(
      { source: "  console  ", content: "  porte ouverte  " },
      fetcher,
    );

    expect(result).toEqual({ ok: true, message: ACCEPTED_MESSAGE });
    expect(fetcher).toHaveBeenCalledWith(
      "/api/events",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ source: "console", content: "porte ouverte" }),
      }),
    );
  });

  it("rend le message d'erreur de l'API", async () => {
    const fetcher = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            error: "content exceeds 4096 characters",
          }),
          { status: 413, headers: { "Content-Type": "application/json" } },
        ),
    );

    const result = await submitDraft(
      { source: DEFAULT_SOURCE, content: "porte ouverte" },
      fetcher,
    );

    expect(result).toEqual({
      ok: false,
      message: "content exceeds 4096 characters",
    });
  });

  it("rend une erreur lisible quand le réseau échoue", async () => {
    const fetcher = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });

    const result = await submitDraft(
      { source: DEFAULT_SOURCE, content: "porte ouverte" },
      fetcher,
    );

    expect(result).toEqual({
      ok: false,
      message: "Impossible de joindre l'API de la Console.",
    });
  });
});
