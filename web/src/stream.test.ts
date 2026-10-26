import { describe, expect, it } from "vitest";

import { applyStreamFrame, initialStreamState } from "./stream";
import type { StatusData } from "./status";
import type { HelloFrame, StatusFrame, TraceEntry, TraceFrame } from "./trace";

function trace(kind: string): TraceEntry {
  return {
    timestamp: 1_700_000_000,
    source: "core",
    kind,
    level: "info",
    payload: {},
  };
}

function status(state: string, activeClient = ""): StatusData {
  return {
    session: {
      state,
      session_id: "sess-1",
      connection_id: "conn-1",
      active_client_id: activeClient,
    },
    clients: [
      {
        client_id: activeClient,
        client_name: activeClient,
        platform: "linux",
      },
    ],
    plugins: [],
    config: {},
  };
}

function hello(backlog: TraceEntry[], snapshot: StatusData): HelloFrame {
  return {
    v: 1,
    type: "hello",
    data: { snapshot: { ...snapshot }, backlog },
  };
}

describe("applyStreamFrame", () => {
  it("un hello remplace l'état et le log d'une connexion précédente", () => {
    const previous = initialStreamState();
    previous.status = status("Idle");
    previous.feed = { entries: [trace("Ancienne")], truncated: 3 };

    const next = applyStreamFrame(previous, hello([trace("Nouvelle")], status("SessionVisible", "c-2")));

    expect(next.status).toEqual(status("SessionVisible", "c-2"));
    expect(next.feed.entries.map((entry) => entry.kind)).toEqual(["Nouvelle"]);
    expect(next.feed.truncated).toBe(0);
  });

  it("une trame status met l'état à jour sans toucher au flux", () => {
    const state = applyStreamFrame(initialStreamState(), hello([], status("Idle")));
    const withTrace = applyStreamFrame(state, {
      v: 1,
      type: "trace",
      data: trace("Live"),
    } as TraceFrame);

    const frame: StatusFrame = {
      v: 1,
      type: "status",
      data: status("SessionVisible", "c-2"),
    };
    const next = applyStreamFrame(withTrace, frame);

    expect(next.status).toEqual(frame.data);
    expect(next.feed).toBe(withTrace.feed);
  });

  it("une trame trace ajoute au log sans toucher à l'état", () => {
    const state = applyStreamFrame(initialStreamState(), hello([], status("Idle")));

    const next = applyStreamFrame(state, {
      v: 1,
      type: "trace",
      data: trace("Live"),
    });

    expect(next.status).toBe(state.status);
    expect(next.feed.entries.map((entry) => entry.kind)).toEqual(["Live"]);
  });

  it("ignore un type de trame inconnu plutôt que de vider la page", () => {
    const state = applyStreamFrame(initialStreamState(), hello([], status("Idle")));
    const next = applyStreamFrame(state, {
      v: 1,
      type: "inconnu",
    } as unknown as TraceFrame);
    expect(next).toBe(state);
  });
});
