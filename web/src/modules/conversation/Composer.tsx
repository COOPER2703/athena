import { useEffect, useId, useRef, type KeyboardEvent } from "react";

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

/** Hauteur maximale du champ, au-delà de laquelle il défile. */
const MAX_TEXTAREA_HEIGHT = 160;

/** En dessous de ce reste, le compteur reparaît : il devient utile. */
const COUNTER_THRESHOLD = 200;

/** En dessous de ce reste, le compteur passe en ton d'avertissement. */
const CRITICAL_THRESHOLD = 50;

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
 * Une seule ligne : le champ respire et s'agrandit avec le texte, les
 * commandes tiennent dans une rangée d'icônes (sourdine, voix, envoi). Aucun
 * libellé visible ne répète ce que l'icône dit ; les noms accessibles et l'aide
 * clavier restent pour les lecteurs d'écran et au survol. Entrée envoie,
 * Maj+Entrée revient à la ligne ; le bouton devient « Interrompre » pendant un
 * Tour et referme la réponse.
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
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const remaining = MAX_MESSAGE_CHARS - value.length;
  const nearLimit = remaining < COUNTER_THRESHOLD;
  const critical = remaining < CRITICAL_THRESHOLD;

  // Le champ suit son contenu sans barre de défilement avant la limite.
  useEffect(() => {
    const field = textareaRef.current;
    if (field === null) {
      return;
    }
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, [value]);

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
      className={cn(
        "flex items-end gap-2 rounded-sm border border-border bg-surface p-2",
        "transition-quiet hover:border-border-strong focus-within:border-accent",
      )}
      onSubmit={(event) => {
        event.preventDefault();
        if (!streaming) {
          onSend();
        }
      }}
    >
      <label htmlFor={id} className="sr-only">
        Message
      </label>
      <textarea
        id={id}
        ref={textareaRef}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        maxLength={MAX_MESSAGE_CHARS}
        rows={1}
        placeholder="Écrivez à Athena…"
        title="Entrée envoie, Maj+Entrée revient à la ligne."
        aria-describedby={`${hintId} ${countId}`}
        style={{ maxHeight: MAX_TEXTAREA_HEIGHT }}
        className={cn(
          "min-h-9 w-full min-w-0 flex-1 resize-none bg-transparent px-1 py-1.5",
          "text-sm leading-relaxed text-text placeholder:text-subtle",
        )}
      />

      {/* Le décompte reste lisible par les lecteurs d'écran en permanence ;
          il n'apparaît à l'écran que lorsqu'il devient utile. */}
      <span id={countId} className="sr-only">
        Caractères restants : {remaining}
      </span>
      {nearLimit ? (
        <span
          aria-hidden="true"
          title="Caractères restants"
          className={cn(
            "shrink-0 pb-1 font-mono text-[0.625rem] tabular-nums",
            critical ? "text-warning" : "text-subtle",
          )}
        >
          {remaining}
        </span>
      ) : null}

      <div className="flex shrink-0 items-center gap-1">
        <Button
          variant="ghost"
          size="square"
          aria-label={muted ? "Activer le son" : "Couper le son"}
          title={muted ? "Activer le son" : "Couper le son"}
          onClick={onToggleMute}
          icon={muted ? <SoundOffIcon /> : <SoundOnIcon />}
        />

        <Button
          variant="secondary"
          size="square"
          aria-label="Parler"
          title="Démarrer la conversation vocale"
          onClick={onStartVoice}
          icon={<MicrophoneIcon />}
        />

        <Button
          type={streaming ? "button" : "submit"}
          variant={streaming ? "danger" : "primary"}
          size="square"
          aria-label={streaming ? "Interrompre" : "Envoyer"}
          title={streaming ? "Interrompre" : "Envoyer"}
          onClick={streaming ? onCancel : undefined}
          disabled={!streaming && value.trim() === ""}
          icon={streaming ? <StopIcon /> : <SendIcon />}
        />
      </div>

      <p id={hintId} className="sr-only">
        Entrée envoie, Maj+Entrée revient à la ligne.
      </p>
    </form>
  );
}
