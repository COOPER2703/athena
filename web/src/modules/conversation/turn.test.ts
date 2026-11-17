import { describe, expect, it } from "vitest";

import { createSseParser, type SseEvent } from "../../api/sse";
import {
  applyTourEvent,
  cancelTurn,
  decodeTourFrame,
  doneReasonLabel,
  finishTurn,
  startTurn,
  type TourEvent,
} from "./turn";

function frame(event: string, data: unknown): SseEvent {
  return { event, data: typeof data === "string" ? data : JSON.stringify(data) };
}

function apply(state: ReturnType<typeof startTurn>, event: TourEvent) {
  return applyTourEvent(state, event);
}

describe("startTurn", () => {
  it("part en diffusion avec le message de l'utilisateur et une réponse vide", () => {
    expect(startTurn("Bonjour")).toMatchObject({
      message: "Bonjour",
      reply: "",
      tools: [],
      phase: "streaming",
      finished: false,
      reason: null,
      errorCode: null,
    });
  });
});

describe("applyTourEvent", () => {
  it("conserve le texte déjà reçu et l'allonge morceau par morceau", () => {
    let turn = startTurn("Compte jusqu'à trois");
    turn = apply(turn, { type: "text", text: "Un, " });
    turn = apply(turn, { type: "text", text: "deux, trois." });

    expect(turn.reply).toBe("Un, deux, trois.");
    expect(turn.phase).toBe("streaming");
    expect(turn.finished).toBe(false);
  });

  it("montre un Tool avec son nom, ses arguments et son statut", () => {
    const turn = apply(startTurn("Mémorise"), {
      type: "tool",
      name: "save_memory",
      arguments: { content: "j'aime le thé", kind: "fact" },
      status: "ok",
    });

    expect(turn.tools).toHaveLength(1);
    expect(turn.tools[0]).toMatchObject({
      name: "save_memory",
      status: "ok",
    });
    expect(turn.tools[0]?.arguments).toContain("j'aime le thé");
    expect(turn.tools[0]?.arguments).toContain("content");
  });

  it("clôt le Tour sur done et n'ajoute plus rien ensuite", () => {
    let turn = apply(startTurn("Bonjour"), { type: "text", text: "Salut." });
    turn = apply(turn, { type: "done", reason: "completed" });
    turn = apply(turn, { type: "text", text: " texte tardif" });

    expect(turn).toMatchObject({ phase: "done", finished: true });
    expect(turn.reason).toBe("completed");
    expect(turn.reply).toBe("Salut.");
  });

  it("explique une panne du modèle par son code stable", () => {
    const turn = apply(startTurn("Bonjour"), {
      type: "error",
      code: "llm_failed",
    });

    expect(turn).toMatchObject({
      phase: "error",
      finished: true,
      errorCode: "llm_failed",
    });
  });

  it("marque le refus « occupée » comme un Tour terminé, pas comme une panne", () => {
    const turn = apply(startTurn("Bonjour"), { type: "busy" });

    expect(turn).toMatchObject({
      phase: "busy",
      finished: true,
      errorCode: "busy",
    });
  });

  it("ignore un événement inconnu sans toucher à l'état", () => {
    const turn = startTurn("Bonjour");
    expect(apply(turn, { type: "unknown" })).toEqual(turn);
  });
});

describe("fin de flux et annulation", () => {
  it("traite une fin de flux sans événement terminal comme une fermeture", () => {
    const turn = finishTurn(apply(startTurn("Bonjour"), { type: "text", text: "Salut." }));

    expect(turn).toMatchObject({ phase: "done", finished: true });
    expect(turn.reason).toBe("closed");
    expect(turn.reply).toBe("Salut.");
  });

  it("conserve le texte partiel d'un Tour interrompu par l'utilisateur", () => {
    const turn = cancelTurn(
      apply(startTurn("Raconte"), { type: "text", text: "Il était une" }),
    );

    expect(turn).toMatchObject({ phase: "canceled", finished: true });
    expect(turn.reply).toBe("Il était une");
  });

  it("ne rouvre jamais un Tour déjà terminé", () => {
    const done = apply(startTurn("Bonjour"), {
      type: "done",
      reason: "completed",
    });
    expect(cancelTurn(done)).toEqual(done);
  });

  it("nomme la raison de fin en français", () => {
    expect(doneReasonLabel("completed")).toBe("Terminé");
    expect(doneReasonLabel("canceled")).toBe("Interrompu");
    expect(doneReasonLabel("timeout")).toBe("Délai dépassé");
    expect(doneReasonLabel("closed")).toBe("Connexion fermée");
    expect(doneReasonLabel("autre")).toBe("Terminé");
  });
});

describe("decodeTourFrame", () => {
  it("lit un morceau de texte", () => {
    expect(decodeTourFrame(frame("text", { text: "Bonjour" }))).toEqual({
      type: "text",
      text: "Bonjour",
    });
  });

  it("lit un Tool et garde ses arguments lisibles", () => {
    const event = decodeTourFrame(
      frame("tool", {
        name: "save_memory",
        arguments: { content: "thé" },
        status: "error",
      }),
    );

    expect(event).toMatchObject({
      type: "tool",
      name: "save_memory",
      status: "error",
    });
  });

  it("décode l'audio PCM en octets", () => {
    const event = decodeTourFrame(
      frame("audio", { data: "AAAAAA==", mime_type: "audio/pcm;rate=24000" }),
    );

    expect(event).toMatchObject({ type: "audio" });
    if (event.type === "audio") {
      expect([...event.pcm]).toEqual([0, 0, 0, 0]);
    }
  });

  it("lit done, error et busy", () => {
    expect(decodeTourFrame(frame("done", { reason: "completed" }))).toEqual({
      type: "done",
      reason: "completed",
    });
    expect(decodeTourFrame(frame("error", { error: "llm_failed" }))).toEqual({
      type: "error",
      code: "llm_failed",
    });
    expect(decodeTourFrame(frame("busy", { error: "busy" }))).toEqual({
      type: "busy",
    });
  });

  it("ignore une trame illisible ou mal typée", () => {
    expect(decodeTourFrame({ event: "text", data: "pas du JSON" })).toEqual({
      type: "unknown",
    });
    expect(decodeTourFrame(frame("text", { text: 42 }))).toEqual({
      type: "unknown",
    });
    expect(decodeTourFrame(frame("inconnu", { x: 1 }))).toEqual({
      type: "unknown",
    });
  });
});

describe("flux complet d'un Tour", () => {
  it("reconstitue texte, audio, Tool et fin depuis des trames brutes", () => {
    const raw = [
      ": keep-alive\n\n",
      'event: tool\ndata: {"name":"save_memory","arguments":{"content":"thé"},"status":"ok"}\n\n',
      'event: audio\ndata: {"data":"AAAAAA==","mime_type":"audio/pcm;rate=24000"}\n\n',
      'event: text\ndata: {"text":"Avec "}\n\n',
      'event: text\ndata: {"text":"plaisir."}\n\n',
      'event: done\ndata: {"reason":"completed"}\n\n',
    ].join("");

    const parser = createSseParser();
    let turn = startTurn("Bonjour");
    const audio: number[] = [];
    for (const frame of parser.push(raw)) {
      const event = decodeTourFrame(frame);
      if (event.type === "audio") {
        audio.push(...event.pcm);
        continue;
      }
      turn = applyTourEvent(turn, event);
    }

    expect(turn.reply).toBe("Avec plaisir.");
    expect(turn.phase).toBe("done");
    expect(turn.tools.map((tool) => tool.name)).toEqual(["save_memory"]);
    expect(audio).toEqual([0, 0, 0, 0]);
  });
});
