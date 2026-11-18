import { CloseIcon, RefreshIcon } from "../../shared/icons";
import { Button } from "../../shared/ui/Button";
import {
  VOICE_INTERRUPTED_NOTICE,
  VOICE_RECONNECTING_NOTICE,
  voicePhaseLabel,
  type VoiceState,
} from "./voice";
import { VoiceWaveform } from "./VoiceWaveform";

export interface VoiceBarProps {
  state: VoiceState;
  getLevel: () => number | null;
  onStop: () => void;
  onResume: () => void;
}

/**
 * Barre vocale : l'onde, l'état visible et une croix.
 *
 * Elle remplace entièrement la barre de saisie et la sourdine : pendant la
 * conversation, la croix est le seul contrôle, et l'état change sans voler le
 * focus. Une coupure affiche la tentative automatique, puis « Reprendre ».
 */
export function VoiceBar({ state, getLevel, onStop, onResume }: VoiceBarProps) {
  const phase = state.phase;
  const active = phase === "listening" || phase === "replying";
  const interrupted =
    state.problem !== null &&
    state.problem.code !== "voice_cut" &&
    state.problem.message !== ""
      ? state.problem.message
      : VOICE_INTERRUPTED_NOTICE;

  return (
    <section
      aria-label="Conversation vocale"
      className="flex flex-col gap-3 rounded-sm border border-accent-line bg-accent-soft p-3 sm:p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <VoiceWaveform getLevel={getLevel} active={active} />
          <div className="min-w-0">
            <p
              role="status"
              aria-live="polite"
              className="font-mono text-xs uppercase tracking-[0.14em] text-accent"
            >
              {voicePhaseLabel(phase)}
            </p>
            <p className="text-xs leading-relaxed text-muted">
              {phaseHint(state)}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {phase === "interrupted" ? (
            <Button
              variant="secondary"
              size="sm"
              onClick={onResume}
              icon={<RefreshIcon />}
            >
              Reprendre
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="sm"
            onClick={onStop}
            title="Terminer la conversation"
            icon={<CloseIcon />}
          >
            Terminer
          </Button>
        </div>
      </div>

      {phase === "reconnecting" ? (
        <p role="status" aria-live="polite" className="text-xs text-warning">
          {VOICE_RECONNECTING_NOTICE}
        </p>
      ) : null}

      {phase === "interrupted" ? (
        <p role="status" aria-live="polite" className="text-xs text-warning">
          {interrupted}
        </p>
      ) : null}
    </section>
  );
}

function phaseHint(state: VoiceState): string {
  switch (state.phase) {
    case "connecting":
      return "Ouverture de la liaison et du micro…";
    case "listening":
      return "Parlez librement : Athena vous écoute.";
    case "replying":
      return "Parlez pour l'interrompre.";
    case "reconnecting":
      return "Le micro reste ouvert ; une seule tentative automatique.";
    case "interrupted":
      return "Le micro reste ouvert pour reprendre sans redemander la permission.";
    default:
      return "";
  }
}
