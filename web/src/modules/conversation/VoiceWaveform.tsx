import { useEffect, useRef } from "react";

import { useTheme } from "../../design/ThemeProvider";

export interface VoiceWaveformProps {
  /** Niveau RMS courant, ou `null` quand l'analyse n'est pas disponible. */
  getLevel: () => number | null;
  /** Faux hors écoute : l'onde reste alors statique, sans animation. */
  active: boolean;
}

/** Nombre de barres : assez pour lire un niveau, assez peu pour rester sobre. */
const BARS = 40;
/** Niveau fixe quand le mouvement est réduit ou l'analyse indisponible. */
const STATIC_LEVEL = 0.34;

/**
 * Onde de la Conversation vocale.
 *
 * Elle lit le niveau réel du micro et ne porte aucune information sans le
 * libellé voisin : le dessin est décoratif (`aria-hidden`). Avec
 * `prefers-reduced-motion`, ou si l'analyse échoue, elle montre un niveau
 * statique plutôt qu'une animation qui ne dit rien de plus.
 */
export function VoiceWaveform({ getLevel, active }: VoiceWaveformProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { theme } = useTheme();

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) {
      return;
    }
    const context = canvas.getContext("2d");
    if (context === null) {
      return;
    }

    const styles = getComputedStyle(canvas);
    const accent = styles.getPropertyValue("--accent").trim() || "#9fe8c0";
    const line = styles.getPropertyValue("--accent-line").trim() || "#2f4a3d";
    const reduced =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let smoothed = 0;
    let frame = 0;

    const draw = (level: number, timestamp: number) => {
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
      const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
      if (canvas.width !== width) {
        canvas.width = width;
      }
      if (canvas.height !== height) {
        canvas.height = height;
      }
      context.clearRect(0, 0, width, height);

      context.strokeStyle = line;
      context.lineWidth = ratio;
      context.beginPath();
      context.moveTo(0, height / 2);
      context.lineTo(width, height / 2);
      context.stroke();

      const gap = width / BARS;
      const barWidth = Math.max(ratio, gap * 0.34);
      context.fillStyle = accent;
      for (let index = 0; index < BARS; index += 1) {
        const position = (index + 0.5) / BARS;
        const envelope = Math.sin(Math.PI * position);
        const wobble =
          0.55 + 0.45 * Math.sin(position * Math.PI * 3 + timestamp / 260);
        const amplitude = Math.max(0.05, level * envelope * wobble);
        const barHeight = Math.max(2 * ratio, amplitude * height * 0.92);
        context.fillRect(
          index * gap + (gap - barWidth) / 2,
          (height - barHeight) / 2,
          barWidth,
          barHeight,
        );
      }
    };

    if (reduced || !active) {
      draw(STATIC_LEVEL, 0);
      return;
    }

    const animate = (timestamp: number) => {
      let level: number | null = null;
      try {
        level = getLevel();
      } catch {
        level = null;
      }
      if (level === null) {
        // Repli statique : l'onde ne prétend pas capter quand elle ne peut pas.
        draw(STATIC_LEVEL, timestamp);
      } else {
        const target = Math.max(0, Math.min(1, level));
        smoothed = smoothed * 0.72 + target * 0.28;
        draw(smoothed, timestamp);
      }
      frame = requestAnimationFrame(animate);
    };
    frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [active, getLevel, theme]);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className="h-10 w-40 shrink-0 sm:w-56"
    />
  );
}
