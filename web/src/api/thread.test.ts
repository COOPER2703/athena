import { describe, expect, it, vi } from "vitest";

import {
  flattenThread,
  fetchThread,
  normalizeThreadPage,
  type ThreadPage,
} from "./thread";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function page(ids: readonly number[]): ThreadPage {
  return {
    entries: ids.map((id) => ({
      id,
      role: "user",
      origin: "text" as const,
      text: `message ${id}`,
      createdAt: "2026-11-17T10:00:00Z",
    })),
    nextBefore: ids[0] ?? null,
    retentionDays: 30,
  };
}

describe("normalizeThreadPage", () => {
  it("lit les entrées, le curseur et la rétention", () => {
    expect(
      normalizeThreadPage({
        v: 1,
        entries: [
          {
            id: 12,
            role: "user",
            origin: "text",
            text: "Bonjour",
            created_at: "2026-11-17T10:00:00Z",
          },
          {
            id: 13,
            role: "assistant",
            origin: "voice",
            text: "Salut.",
            created_at: "2026-11-17T10:00:03Z",
          },
        ],
        next_before: 12,
        retention_days: 30,
      }),
    ).toEqual({
      entries: [
        {
          id: 12,
          role: "user",
          origin: "text",
          text: "Bonjour",
          createdAt: "2026-11-17T10:00:00Z",
        },
        {
          id: 13,
          role: "assistant",
          origin: "voice",
          text: "Salut.",
          createdAt: "2026-11-17T10:00:03Z",
        },
      ],
      nextBefore: 12,
      retentionDays: 30,
    });
  });

  it("ignore une entrée sans identifiant : elle ne serait pas paginable", () => {
    const body = normalizeThreadPage({
      entries: [{ id: "12", text: "sans identifiant" }, { id: 4, text: "ok" }],
      retention_days: 30,
    });
    expect(body.entries.map((entry) => entry.id)).toEqual([4]);
  });

  it("marque une origine inconnue sans la confondre avec le clavier", () => {
    const body = normalizeThreadPage({
      entries: [{ id: 4, role: "assistant", origin: "autre", text: "x" }],
    });
    expect(body.entries[0]?.origin).toBe("unknown");
  });

  it("retombe sur une page vide plutôt que de casser le fil", () => {
    expect(normalizeThreadPage(null)).toEqual({
      entries: [],
      nextBefore: null,
      retentionDays: 0,
    });
    expect(normalizeThreadPage({ retention_days: -4 }).retentionDays).toBe(0);
  });
});

describe("flattenThread", () => {
  it("remet les pages du plus ancien au plus récent", () => {
    expect(flattenThread([page([3, 4]), page([1, 2])]).map((e) => e.id)).toEqual([
      1, 2, 3, 4,
    ]);
  });

  it("ne montre jamais deux fois la même entrée", () => {
    expect(
      flattenThread([page([3, 4]), page([1, 2, 3])]).map((e) => e.id),
    ).toEqual([1, 2, 3, 4]);
  });

  it("rend un fil vide sans page chargée", () => {
    expect(flattenThread(undefined)).toEqual([]);
    expect(flattenThread([])).toEqual([]);
  });
});

describe("fetchThread", () => {
  it("demande la première page sans curseur", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1, entries: [], retention_days: 30 }),
    );

    await fetchThread({}, fetcher);

    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/thread?limit=50");
  });

  it("pagine vers le passé avec le curseur du serveur", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(200, { v: 1, entries: [], retention_days: 30 }),
    );

    await fetchThread({ before: 42, limit: 20 }, fetcher);

    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/thread?limit=20&before=42");
  });

  it("traduit un refus en ApiError", async () => {
    const fetcher = vi.fn(async (_path: string, _init: RequestInit) =>
      response(503, { v: 1, error: "thread_unavailable", message: "non" }),
    );

    await expect(fetchThread({}, fetcher)).rejects.toMatchObject({
      status: 503,
      code: "thread_unavailable",
    });
  });
});
