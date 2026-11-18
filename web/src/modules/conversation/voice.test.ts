import { describe, expect, it } from "vitest";

import {
  applyVoiceEvent,
  bargeInDetected,
  decodeVoiceFrame,
  idleVoice,
  VOICE_INTERRUPTED_NOTICE,
  VOICE_RECONNECTING_NOTICE,
  voiceBarVisible,
  voiceEndMessage,
  voiceEndStatus,
  voiceEndTitle,
  voicePhaseLabel,
  type VoiceEvent,
  type VoiceState,
} from "./voice";

/** Applique une suite d'événements au départ d'une conversation neuve. */
function run(...events: readonly VoiceEvent[]): VoiceState {
  return events.reduce(applyVoiceEvent, idleVoice());
}

function listening(): VoiceState {
  return run({ type: "start" }, { type: "ready" });
}

describe("démarrage d'une conversation", () => {
  it("part en connexion, sans ligne ni motif de fin", () => {
    expect(idleVoice()).toMatchObject({
      phase: "idle",
      lines: [],
      automaticReconnectUsed: false,
      endReason: null,
      problem: null,
    });
    expect(run({ type: "start" })).toMatchObject({
      phase: "connecting",
      lines: [],
      automaticReconnectUsed: false,
      endReason: null,
      problem: null,
    });
  });

  it("n'ouvre « Parlez » qu'une fois la trame ready reçue", () => {
    const state = listening();
    expect(state.phase).toBe("listening");
    expect(voicePhaseLabel(state.phase)).toBe("Parlez");
  });

  it("ignore un second démarrage pendant une conversation", () => {
    const state = listening();
    expect(applyVoiceEvent(state, { type: "start" })).toBe(state);
  });

  it("recommence proprement après une fin, sans garder l'ancien motif", () => {
    const ended = run({ type: "start" }, { type: "stop" });
    const restarted = applyVoiceEvent(ended, { type: "start" });

    expect(restarted).toMatchObject({
      phase: "connecting",
      lines: [],
      automaticReconnectUsed: false,
      endReason: null,
      problem: null,
    });
  });
});

describe("lignes transitoires", () => {
  it("retient le repère du Journal au démarrage", () => {
    expect(idleVoice().journalBaseline).toBe(0);
    expect(run({ type: "start", baselineId: 42 }).journalBaseline).toBe(42);
  });

  it("oublie les lignes que le Journal vient de reprendre", () => {
    // Après une relecture du fil, les tours confirmés n'ont plus à rester en
    // mémoire : le Journal les affiche, la ligne vivante suivante reste seule.
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "transcript", role: "user", text: "Bonjour" },
      { type: "transcript", role: "assistant", text: "Salut." },
      { type: "transcript", role: "user", text: "Encore" },
      { type: "settle", drop: 2 },
    );

    expect(state.lines).toEqual([{ role: "user", text: "Encore" }]);
  });

  it("ne retire jamais plus de lignes qu'il n'en existe", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "transcript", role: "user", text: "Bonjour" },
      { type: "settle", drop: 5 },
    );

    expect(state.lines).toEqual([]);
  });
});

describe("transcriptions et réponse", () => {
  it("ajoute les mots de l'utilisateur et repasse en écoute", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "audio" },
      { type: "transcript", role: "user", text: "Bonjour Athena" },
    );

    expect(state.lines).toEqual([
      { role: "user", text: "Bonjour Athena" },
    ]);
    expect(state.phase).toBe("listening");
  });

  it("affiche la réponse d'Athena et passe en « Athena répond »", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "transcript", role: "assistant", text: "Bonjour." },
    );

    expect(state.lines).toEqual([
      { role: "assistant", text: "Bonjour." },
    ]);
    expect(voicePhaseLabel(state.phase)).toBe("Athena répond");
  });

  it("passe en réponse dès le premier morceau audio", () => {
    const replying = run({ type: "start" }, { type: "ready" }, { type: "audio" });
    expect(replying.phase).toBe("replying");
    // Les morceaux suivants ne provoquent aucun rendu supplémentaire.
    expect(applyVoiceEvent(replying, { type: "audio" })).toBe(replying);
  });

  it("ignore une transcription vide sans changer d'état", () => {
    const state = listening();
    expect(
      applyVoiceEvent(state, { type: "transcript", role: "user", text: "" }),
    ).toBe(state);
  });

  it("garde toutes les lignes dans l'ordre, utilisateur puis Athena", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "transcript", role: "user", text: "Raconte une histoire" },
      { type: "transcript", role: "assistant", text: "Il était une fois." },
      { type: "transcript", role: "user", text: "Encore" },
    );

    expect(state.lines.map((line) => line.role)).toEqual([
      "user",
      "assistant",
      "user",
    ]);
  });
});

describe("fin de conversation", () => {
  it("termine par la croix avec un motif propre et distinct", () => {
    const state = run({ type: "start" }, { type: "ready" }, { type: "stop" });

    expect(state).toMatchObject({ phase: "ended", endReason: "user" });
    expect(voiceEndTitle("user")).toBe("Conversation terminée");
    expect(voiceEndMessage("user")).toContain("Vous avez terminé");
  });

  it("termine proprement quand le système suspend le micro mobile", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "background" },
    );

    expect(state).toMatchObject({ phase: "ended", endReason: "background" });
    expect(voiceEndMessage("background")).toContain("arrière-plan");
  });

  it("lit chaque motif du protocole et donne une phrase distincte", () => {
    const reasons = [
      "COMPLETED",
      "TIMEOUT",
      "CLIENT_GONE",
      "NORMAL",
      "ERROR",
      "SHUTDOWN",
      "REPLACED",
    ] as const;
    const messages = reasons.map((reason) => {
      const state = run(
        { type: "start" },
        { type: "ready" },
        { type: "ended", reason },
      );
      expect(state.phase).toBe("ended");
      return voiceEndMessage(state.endReason ?? "normal");
    });

    expect(new Set(messages).size).toBe(reasons.length);
    expect(
      run({ type: "start" }, { type: "ready" }, { type: "ended", reason: "COMPLETED" })
        .endReason,
    ).toBe("completed");
  });

  it("retient le motif timeout et le distingue de la croix", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "ended", reason: "TIMEOUT" },
    );

    expect(state.endReason).toBe("timeout");
    expect(voiceEndMessage("timeout")).toContain("inactivité");
    expect(voiceEndMessage("timeout")).not.toBe(voiceEndMessage("user"));
  });

  it("retient un refus serveur avec son code et sa phrase", () => {
    const state = run(
      { type: "start" },
      { type: "refused", code: "session_busy", message: "Une autre conversation est déjà en cours." },
    );

    expect(state.phase).toBe("refused");
    expect(state.problem).toEqual({
      code: "session_busy",
      message: "Une autre conversation est déjà en cours.",
    });
    expect(voicePhaseLabel(state.phase)).toBe("Refusée");
  });

  it("ne rouvre jamais une conversation terminée sur une trame tardive", () => {
    const ended = run({ type: "start" }, { type: "ready" }, { type: "stop" });
    expect(applyVoiceEvent(ended, { type: "audio" })).toBe(ended);
    expect(
      applyVoiceEvent(ended, { type: "transcript", role: "user", text: "tard" }),
    ).toBe(ended);
    expect(applyVoiceEvent(ended, { type: "ended", reason: "TIMEOUT" })).toBe(
      ended,
    );
  });

  it("efface le motif de fin quand l'écran ferme le bandeau", () => {
    const ended = run({ type: "start" }, { type: "ready" }, { type: "stop" });
    expect(applyVoiceEvent(ended, { type: "dismiss" })).toEqual(idleVoice());
  });
});

describe("coupure et reconnexion", () => {
  it("tente une reconnexion automatique après une coupure en conversation", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "transcript", role: "user", text: "Bonjour" },
      { type: "cut" },
    );

    expect(state).toMatchObject({
      phase: "reconnecting",
      automaticReconnectUsed: true,
    });
    expect(voicePhaseLabel(state.phase)).toBe("Reconnexion…");
    expect(VOICE_RECONNECTING_NOTICE).toBe(
      "Conversation interrompue — tentative de reconnexion en cours",
    );
  });

  it("s'arrête sur « Interrompue » quand la tentative automatique échoue", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "cut" },
      { type: "cut" },
    );

    expect(state.phase).toBe("interrupted");
    expect(voicePhaseLabel(state.phase)).toBe("Interrompue");
    expect(VOICE_INTERRUPTED_NOTICE).toContain("Reprendre");
  });

  it("ne tente jamais une seconde reconnexion automatique", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "cut" },
      { type: "ready" },
      { type: "cut" },
    );

    expect(state.phase).toBe("interrupted");
    expect(state.automaticReconnectUsed).toBe(true);
  });

  it("refuse la conversation quand la première liaison n'aboutit jamais", () => {
    const state = run({ type: "start" }, { type: "cut" });

    expect(state).toMatchObject({
      phase: "refused",
      automaticReconnectUsed: false,
      problem: { code: "voice_connect_failed" },
    });
  });

  it("reprend à la demande en gardant les lignes et en réarmant la tentative unique", () => {
    const interrupted = run(
      { type: "start" },
      { type: "ready" },
      { type: "transcript", role: "user", text: "Bonjour" },
      { type: "cut" },
      { type: "cut" },
    );
    const resumed = applyVoiceEvent(interrupted, { type: "resume" });

    expect(resumed).toMatchObject({
      phase: "connecting",
      automaticReconnectUsed: false,
    });
    expect(resumed.lines).toEqual(interrupted.lines);
  });

  it("passe de la reconnexion à l'écoute quand le socket se rouvre", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "cut" },
      { type: "ready" },
    );

    expect(state.phase).toBe("listening");
    expect(state.automaticReconnectUsed).toBe(true);
  });

  it("explique une panne de reprise sans perdre les lignes", () => {
    const state = run(
      { type: "start" },
      { type: "ready" },
      { type: "transcript", role: "user", text: "Bonjour" },
      { type: "cut" },
      { type: "failed", code: "microphone_lost", message: "Le micro n'est plus ouvert." },
    );

    expect(state).toMatchObject({
      phase: "interrupted",
      problem: { code: "microphone_lost" },
    });
    expect(state.lines).toHaveLength(1);
  });

  it("refuse la reprise quand le micro n'est plus là, sans perdre les lignes", () => {
    const interrupted = run(
      { type: "start" },
      { type: "ready" },
      { type: "transcript", role: "user", text: "Bonjour" },
      { type: "cut" },
      { type: "cut" },
    );
    const refused = applyVoiceEvent(interrupted, {
      type: "failed",
      code: "microphone_lost",
      message: "Le micro n'est plus ouvert.",
    });

    expect(refused).toMatchObject({
      phase: "refused",
      problem: { code: "microphone_lost" },
    });
    expect(refused.lines).toEqual(interrupted.lines);
  });

  it("refuse proprement quand la capture échoue au démarrage", () => {
    const state = run(
      { type: "start" },
      { type: "failed", code: "microphone_denied", message: "Micro refusé." },
    );

    expect(state).toMatchObject({
      phase: "refused",
      problem: { code: "microphone_denied", message: "Micro refusé." },
    });
  });
});

describe("barre vocale", () => {
  it("remplace la barre de saisie pendant toute la conversation", () => {
    expect(voiceBarVisible("connecting")).toBe(true);
    expect(voiceBarVisible("listening")).toBe(true);
    expect(voiceBarVisible("replying")).toBe(true);
    expect(voiceBarVisible("reconnecting")).toBe(true);
    expect(voiceBarVisible("interrupted")).toBe(true);
    expect(voiceBarVisible("idle")).toBe(false);
    expect(voiceBarVisible("ended")).toBe(false);
    expect(voiceBarVisible("refused")).toBe(false);
  });

  it("nomme chaque état visible en français", () => {
    expect(voicePhaseLabel("idle")).toBe("");
    expect(voicePhaseLabel("connecting")).toBe("Connexion…");
    expect(voicePhaseLabel("listening")).toBe("Parlez");
    expect(voicePhaseLabel("replying")).toBe("Athena répond");
    expect(voicePhaseLabel("reconnecting")).toBe("Reconnexion…");
    expect(voicePhaseLabel("interrupted")).toBe("Interrompue");
    expect(voicePhaseLabel("ended")).toBe("Terminée");
  });

  it("titre une erreur comme une interruption, pas comme une fin voulue", () => {
    expect(voiceEndTitle("error")).toBe("Conversation interrompue");
    expect(voiceEndTitle("completed")).toBe("Conversation terminée");
    expect(voiceEndStatus("error")).toBe("Interrompue");
    expect(voiceEndStatus("timeout")).toBe("Terminée");
  });
});

describe("barge-in local", () => {
  it("coupe la lecture quand la voix monte pendant la réponse", () => {
    const replying = run({ type: "start" }, { type: "ready" }, { type: "audio" });
    expect(bargeInDetected(replying, 0.25)).toBe(true);
  });

  it("ne coupe rien pendant l'écoute ni sous le seuil", () => {
    const listeningState = listening();
    expect(bargeInDetected(listeningState, 0.25)).toBe(false);
    const replying = run({ type: "start" }, { type: "ready" }, { type: "audio" });
    expect(bargeInDetected(replying, 0.01)).toBe(false);
  });
});

describe("decodeVoiceFrame", () => {
  it("lit ready, transcript et audio", () => {
    expect(
      decodeVoiceFrame({ v: 1, type: "ready", data: {} }),
    ).toEqual({ type: "ready" });
    expect(
      decodeVoiceFrame({
        v: 1,
        type: "transcript",
        data: { role: "user", text: "Bonjour" },
      }),
    ).toEqual({ type: "transcript", role: "user", text: "Bonjour" });
    const audio = decodeVoiceFrame({
      v: 1,
      type: "audio",
      data: { mime: "audio/pcm;rate=24000", data: "AAAAAA==" },
    });
    expect(audio).toMatchObject({ type: "audio" });
    if (audio?.type === "audio") {
      expect([...audio.pcm]).toEqual([0, 0, 0, 0]);
    }
  });

  it("normalise le motif de fin et retient ended et refused", () => {
    expect(
      decodeVoiceFrame({
        v: 1,
        type: "ended",
        data: { reason: "CLIENT_GONE" },
      }),
    ).toEqual({ type: "ended", reason: "client_gone" });
    expect(
      decodeVoiceFrame({
        v: 1,
        type: "ended",
        data: { reason: "INCONNU" },
      }),
    ).toEqual({ type: "ended", reason: "normal" });
    expect(
      decodeVoiceFrame({
        v: 1,
        type: "refused",
        data: { code: "tour_busy", message: "Athena répond déjà." },
      }),
    ).toEqual({
      type: "refused",
      code: "tour_busy",
      message: "Athena répond déjà.",
    });
  });

  it("ignore une version inconnue, une trame mal typée ou un audio non PCM", () => {
    expect(decodeVoiceFrame({ v: 2, type: "ready", data: {} })).toBeNull();
    expect(decodeVoiceFrame({ v: 1, type: "ready" })).toBeNull();
    expect(decodeVoiceFrame({ v: 1, type: "inconnu", data: {} })).toBeNull();
    expect(
      decodeVoiceFrame({
        v: 1,
        type: "transcript",
        data: { role: "system", text: "non" },
      }),
    ).toBeNull();
    expect(
      decodeVoiceFrame({
        v: 1,
        type: "audio",
        data: { mime: "audio/ogg", data: "AAAAAA==" },
      }),
    ).toBeNull();
    expect(decodeVoiceFrame(null)).toBeNull();
    expect(decodeVoiceFrame("pas un objet")).toBeNull();
  });
});
