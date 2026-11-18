import { useId, type KeyboardEvent } from "react";

import { MAX_MESSAGE_CHARS } from "../../api/chat";
import { cn } from "../../shared/cn";
import {
  MicrophoneIcon,
  SendIcon,
  SoundOffIcon,
  SoundOnIcon,
  StopIcon,
} from "../../shared/icons";
import { Button } from "../../shared/ui/Button";

export interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onCancel: () => void;
  onStartVoice: () => void;
  streaming: boolean;
  muted: boolean;
  onToggleMute: () => void;
}

/**
 * Barre de saisie de la Conversation.
 *
 * Entrée envoie, Maj+Entrée revient à la ligne ; le bouton devient
 * « Interrompre » pendant un Tour et referme la réponse. Le réglage de sourdine
 * est à portée de clavier et annonce son état.
 */
export function Composer({
  value,
  onChange,
  onSend,
  onCancel,
  onStartVoice,
  streaming,
  muted,
  onToggleMute,
}: ComposerProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const countId = `${id}-count`;
  const remaining = MAX_MESSAGE_CHARS - value.length;

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    if (event.nativeEvent.isComposing) {
      return;
    }
    event.preventDefault();
    if (!streaming) {
      onSend();
    }
  };

  return (
    <form
      className="flex flex-col gap-3 rounded-sm border border-border bg-surface p-3 sm:p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!streaming) {
          onSend();
        }
      }}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <label
          htmlFor={id}
          className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
        >
          Message
        </label>
        <span
          id={countId}
          className={cn(
            "font-mono text-[0.625rem] tabular-nums",
            remaining < 200 ? "text-warning" : "text-subtle",
          )}
        >
          {value.length} / {MAX_MESSAGE_CHARS}
        </span>
      </div>

      <textarea
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        maxLength={MAX_MESSAGE_CHARS}
        rows={3}
        placeholder="Écrivez à Athena…"
        aria-describedby={`${hintId} ${countId}`}
        className={cn(
          "min-h-[5.5rem] w-full resize-y rounded-sm border border-border bg-canvas px-3 py-2.5",
          "text-sm leading-relaxed text-text placeholder:text-subtle",
          "transition-quiet hover:border-border-strong focus:border-accent",
        )}
      />

      <p id={hintId} className="text-xs leading-relaxed text-subtle">
        Entrée envoie · Maj+Entrée revient à la ligne.
      </p>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            aria-pressed={!muted}
            onClick={onToggleMute}
            icon={muted ? <SoundOffIcon /> : <SoundOnIcon />}
          >
            {muted ? "Son coupé" : "Son actif"}
          </Button>

          <Button
            variant="secondary"
            size="sm"
            onClick={onStartVoice}
            disabled={streaming}
            title={
              streaming
                ? "Attendez la fin de la réponse pour passer à la voix."
                : "Démarrer la conversation vocale"
            }
            icon={<MicrophoneIcon />}
          >
            Parler
          </Button>
        </div>

        <Button
          type={streaming ? "button" : "submit"}
          variant={streaming ? "danger" : "primary"}
          onClick={streaming ? onCancel : undefined}
          disabled={!streaming && value.trim() === ""}
          icon={streaming ? <StopIcon /> : <SendIcon />}
        >
          {streaming ? "Interrompre" : "Envoyer"}
        </Button>
      </div>
    </form>
  );
}
