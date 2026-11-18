import { useCallback, useEffect, useMemo, useRef } from "react";

import { AUDIO_SAMPLE_RATE, pcm16ToFloat32 } from "./audio";

/** Lecture de l'audio d'un Tour, en file et interruptible immédiatement. */
export interface AudioPlayer {
  /** Met un morceau PCM 16 bits 24 kHz en file ; sans contexte actif, ignore. */
  push(pcm: Uint8Array): void;
  /** Coupe le son : les sources en cours et la file sont arrêtées. */
  stop(): void;
  /** Crée ou reprend le contexte sur un geste de l'utilisateur. */
  unlock(): void;
}

type AudioContextConstructor = typeof AudioContext;

/**
 * Joue le PCM descendant du flux.
 *
 * Le navigateur ne laisse démarrer l'audio qu'après un geste : `unlock()` est
 * appelé par le bouton de son et par l'envoi d'un message. Tant que le contexte
 * n'est pas actif, les morceaux sont ignorés plutôt que mis en attente : couper
 * le son ne doit jamais laisser une file prête à repartir.
 */
export function useAudioPlayer(): AudioPlayer {
  const contextRef = useRef<AudioContext | null>(null);
  const sourcesRef = useRef<Set<AudioBufferSourceNode>>(new Set());
  const nextStartRef = useRef(0);

  const ensureContext = useCallback((): AudioContext | null => {
    const current = contextRef.current;
    if (current !== null) {
      return current;
    }
    const Constructor: AudioContextConstructor | undefined =
      window.AudioContext ??
      (window as Window & { webkitAudioContext?: AudioContextConstructor })
        .webkitAudioContext;
    if (Constructor === undefined) {
      return null;
    }
    const context = new Constructor();
    contextRef.current = context;
    return context;
  }, []);

  const unlock = useCallback(() => {
    const context = ensureContext();
    if (context !== null && context.state === "suspended") {
      void context.resume();
    }
  }, [ensureContext]);

  const push = useCallback((pcm: Uint8Array) => {
    const context = contextRef.current;
    // Aucun son ne part avant le geste : la sourdine par défaut n'est pas une
    // file d'attente déguisée.
    if (context === null || context.state !== "running") {
      return;
    }
    const samples = pcm16ToFloat32(pcm);
    if (samples.length === 0) {
      return;
    }
    const buffer = context.createBuffer(1, samples.length, AUDIO_SAMPLE_RATE);
    buffer.copyToChannel(samples, 0);

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    const start = Math.max(context.currentTime + 0.01, nextStartRef.current);
    source.start(start);
    nextStartRef.current = start + buffer.duration;
    sourcesRef.current.add(source);
    source.addEventListener("ended", () => {
      sourcesRef.current.delete(source);
    });
  }, []);

  const stop = useCallback(() => {
    for (const source of sourcesRef.current) {
      try {
        source.stop();
      } catch {
        // Une source déjà terminée refuse d'être arrêtée : rien à faire.
      }
    }
    sourcesRef.current.clear();
    nextStartRef.current = 0;
  }, []);

  useEffect(
    () => () => {
      stop();
      const context = contextRef.current;
      contextRef.current = null;
      if (context !== null) {
        void context.close();
      }
    },
    [stop],
  );

  return useMemo(() => ({ push, stop, unlock }), [push, stop, unlock]);
}
