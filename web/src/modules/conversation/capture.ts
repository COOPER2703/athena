/**
 * Capture du micro de la Conversation vocale (ADR-0027).
 *
 * `getUserMedia` fournit un flux stéréo ou mono ; un `AudioWorklet` le descend
 * en mono, le rééchantillonne à 16 kHz, le quantifie en PCM 16 bits
 * petit-boutiste et émet des trames d'environ 64 ms (1024 échantillons), comme
 * le Client desktop. Le même graphe expose un `AnalyserNode` pour l'onde, sans
 * jamais jouer le micro dans les haut-parleurs.
 *
 * Ce module touche au DOM : la machine à états, elle, reste pure (`voice.ts`).
 */

/** Code stable d'un échec de capture, traduit par `describeCode`. */
export type CaptureFailureCode =
  | "microphone_denied"
  | "microphone_missing"
  | "microphone_unavailable"
  | "insecure_context"
  | "voice_unsupported"
  | "audio_worklet_failed";

/** Échec de capture porteur d'un code stable, jamais d'un message brut. */
export class CaptureError extends Error {
  readonly code: CaptureFailureCode;

  constructor(code: CaptureFailureCode) {
    super(code);
    this.name = "CaptureError";
    this.code = code;
  }
}

/** Ce qu'une capture ouverte met à disposition de la conversation. */
export interface VoiceCapture {
  /** Flux réel du micro, conservé pour une reprise sans nouvelle permission. */
  readonly stream: MediaStream;
  /** Niveau RMS entre 0 et 1, ou `null` si l'analyse n'est pas disponible. */
  readLevel(): number | null;
  /** Coupe les pistes, déconnecte le graphe et ferme le contexte. Idempotent. */
  stop(): void;
}

export interface VoiceCaptureOptions {
  /** Trame PCM 16 bits mono 16 kHz, transférée sans copie. */
  onFrame: (pcm: Uint8Array) => void;
  /**
   * Contexte créé dans le geste d'ouverture, déjà repris.
   *
   * Les navigateurs mobiles suspendent un `AudioContext` créé après une
   * permission asynchrone ; le préparer au tap garde le geste valable.
   */
  context?: AudioContext | null;
}

/**
 * Vérifie le socle navigateur avant de demander le micro.
 *
 * Un refus local est une phrase explicite, jamais un bouton inerte : la Console
 * dit ce qui manque ou ce qu'il faut corriger (HTTPS, navigateur récent).
 */
export function voiceSupport():
  | { ok: true }
  | { ok: false; code: CaptureFailureCode } {
  if (typeof window === "undefined") {
    return { ok: false, code: "voice_unsupported" };
  }
  if (!window.isSecureContext) {
    return { ok: false, code: "insecure_context" };
  }
  if (
    typeof navigator === "undefined" ||
    navigator.mediaDevices === undefined ||
    typeof navigator.mediaDevices.getUserMedia !== "function"
  ) {
    return { ok: false, code: "voice_unsupported" };
  }
  if (
    audioContextConstructor() === undefined ||
    typeof AudioWorkletNode === "undefined"
  ) {
    return { ok: false, code: "voice_unsupported" };
  }
  return { ok: true };
}

/**
 * Prépare le contexte audio **dans le geste** qui démarre la conversation.
 *
 * Aucun micro n'est ouvert ici : seul le contexte de lecture/capture est créé
 * et repris, pour que la permission asynchrone ne casse pas le déblocage sur
 * mobile. `null` laisse `startCapture` en créer un après `ready`.
 */
export function prepareCaptureContext(): AudioContext | null {
  const Constructor = audioContextConstructor();
  if (Constructor === undefined) {
    return null;
  }
  try {
    const context = new Constructor({ latencyHint: "interactive" });
    void context.resume().catch(() => undefined);
    return context;
  } catch {
    return null;
  }
}

/** Ferme un contexte préparé qui n'a pas été confié à une capture. */
export function releaseCaptureContext(context: AudioContext | null): void {
  if (context !== null) {
    void context.close().catch(() => undefined);
  }
}

/** Ouvre le micro et branche le graphe de capture ; rejette un `CaptureError`. */
export async function startCapture(
  options: VoiceCaptureOptions,
): Promise<VoiceCapture> {
  const provided = options.context ?? null;
  const support = voiceSupport();
  if (!support.ok) {
    releaseCaptureContext(provided);
    throw new CaptureError(support.code);
  }
  const Constructor = audioContextConstructor();
  if (Constructor === undefined) {
    releaseCaptureContext(provided);
    throw new CaptureError("voice_unsupported");
  }

  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
  } catch (reason) {
    releaseCaptureContext(provided);
    throw new CaptureError(getUserMediaFailure(reason));
  }

  let context: AudioContext | null = provided;
  let streamEnded = false;
  const releaseStream = () => {
    if (streamEnded) {
      return;
    }
    streamEnded = true;
    for (const track of stream.getTracks()) {
      track.stop();
    }
  };

  try {
    if (context === null) {
      context = new Constructor({ latencyHint: "interactive" });
    }
    if (context.audioWorklet === undefined) {
      throw new CaptureError("voice_unsupported");
    }
    const url = workletUrl();
    try {
      await context.audioWorklet.addModule(url);
    } catch {
      throw new CaptureError("audio_worklet_failed");
    } finally {
      URL.revokeObjectURL(url);
    }
    if (context.state === "suspended") {
      // Le tap d'origine est encore proche : la reprise explicite évite un
      // contexte muet sur les navigateurs qui suspendent à la création.
      await context.resume().catch(() => undefined);
    }

    const source = context.createMediaStreamSource(stream);
    let analyser: AnalyserNode | null = null;
    let samples: Float32Array<ArrayBuffer> | null = null;
    try {
      analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.7;
      samples = new Float32Array(analyser.fftSize);
      source.connect(analyser);
    } catch {
      // L'onde est un confort : une analyse indisponible ne doit pas empêcher
      // de parler, elle affichera seulement un niveau statique.
      analyser = null;
      samples = null;
    }
    const worklet = new AudioWorkletNode(context, PROCESSOR_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: 1,
      channelCountMode: "explicit",
    });
    // Le micro ne doit jamais revenir dans les haut-parleurs : le seul chemin
    // vers la sortie traverse un gain à zéro.
    const mute = context.createGain();
    mute.gain.value = 0;

    source.connect(worklet);
    analyser?.connect(mute);
    worklet.connect(mute);
    mute.connect(context.destination);

    worklet.port.onmessage = (event: MessageEvent) => {
      const data = event.data;
      if (data instanceof ArrayBuffer) {
        options.onFrame(new Uint8Array(data));
      }
    };

    let stopped = false;
    const activeContext = context;

    return {
      stream,
      readLevel(): number | null {
        if (stopped || analyser === null || samples === null) {
          return null;
        }
        try {
          analyser.getFloatTimeDomainData(samples);
        } catch {
          return null;
        }
        let total = 0;
        for (const value of samples) {
          total += value * value;
        }
        const rms = Math.sqrt(total / samples.length);
        // Le RMS brut est petit même pour une voix proche : l'échelle rend
        // l'onde lisible sans amplifier le bruit de fond.
        return Math.min(1, rms * 5);
      },
      stop(): void {
        if (stopped) {
          return;
        }
        stopped = true;
        worklet.port.onmessage = null;
        source.disconnect();
        analyser?.disconnect();
        worklet.disconnect();
        mute.disconnect();
        releaseStream();
        void activeContext.close().catch(() => undefined);
      },
    };
  } catch (reason) {
    releaseStream();
    if (context !== null) {
      void context.close().catch(() => undefined);
    }
    if (reason instanceof CaptureError) {
      throw reason;
    }
    throw new CaptureError("audio_worklet_failed");
  }
}

/** Constructeur standard ou préfixé, sans jamais supposer sa présence. */
function audioContextConstructor(): typeof AudioContext | undefined {
  if (typeof window === "undefined") {
    return undefined;
  }
  return (
    window.AudioContext ??
    (window as Window & { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext
  );
}

/** Une permission refusée n'est pas un micro absent : chaque cause a son code. */
function getUserMediaFailure(reason: unknown): CaptureFailureCode {
  const name =
    typeof reason === "object" && reason !== null && "name" in reason
      ? String((reason as { name?: unknown }).name)
      : "";
  switch (name) {
    case "NotAllowedError":
    case "SecurityError":
    case "PermissionDeniedError":
      return "microphone_denied";
    case "NotFoundError":
    case "DevicesNotFoundError":
      return "microphone_missing";
    default:
      return "microphone_unavailable";
  }
}

const PROCESSOR_NAME = "athena-voice-capture";

function workletUrl(): string {
  return URL.createObjectURL(
    new Blob([WORKLET_SOURCE], { type: "text/javascript" }),
  );
}

/**
 * Source du `AudioWorkletProcessor`.
 *
 * Elle vit dans une chaîne, chargée par URL `blob:` : aucun fichier séparé à
 * copier dans le build, et le même code en développement qu'en production.
 * Le rééchantillonnage est linéaire, avec report de la position fractionnaire
 * d'un bloc au suivant pour ne pas cliquer aux jointures.
 */
const WORKLET_SOURCE = `
class AthenaVoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate;
    this.frame = new Int16Array(1024);
    this.frameIndex = 0;
    this.carry = new Float32Array(0);
    this.position = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) {
      return true;
    }
    const length = input[0] ? input[0].length : 0;
    if (length === 0) {
      return true;
    }
    const mono = new Float32Array(length);
    for (let channel = 0; channel < input.length; channel += 1) {
      const data = input[channel];
      if (!data) {
        continue;
      }
      for (let index = 0; index < length; index += 1) {
        mono[index] += data[index];
      }
    }
    const scale = 1 / input.length;
    for (let index = 0; index < length; index += 1) {
      mono[index] *= scale;
    }

    const buffer = new Float32Array(this.carry.length + mono.length);
    buffer.set(this.carry, 0);
    buffer.set(mono, this.carry.length);

    let position = this.position;
    const output = [];
    while (position + 1 < buffer.length) {
      const index = Math.floor(position);
      const fraction = position - index;
      const first = buffer[index];
      const second = buffer[index + 1];
      output.push(first + (second - first) * fraction);
      position += this.ratio;
    }
    const consumed = Math.min(Math.floor(position), buffer.length);
    this.carry = buffer.slice(consumed);
    this.position = position - consumed;

    for (let index = 0; index < output.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, output[index]));
      this.frame[this.frameIndex] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this.frameIndex += 1;
      if (this.frameIndex === this.frame.length) {
        this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
        this.frame = new Int16Array(1024);
        this.frameIndex = 0;
      }
    }
    return true;
  }
}

registerProcessor("athena-voice-capture", AthenaVoiceCapture);
`;
