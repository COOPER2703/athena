import { useId, type InputHTMLAttributes } from "react";

import { cn } from "../cn";

export interface TextFieldProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "id"> {
  label: string;
  hint?: string;
  error?: string;
}

/** Champ texte avec libellé, aide et erreur reliés par `aria-describedby`. */
export function TextField({
  label,
  hint,
  error,
  className,
  ...props
}: TextFieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;

  return (
    <div className="flex flex-col gap-1.5">
      <label
        htmlFor={id}
        className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
      >
        {label}
      </label>
      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className={cn(
          "h-10 rounded-sm border border-border bg-canvas px-3 font-mono text-sm text-text",
          "placeholder:text-subtle",
          "transition-quiet hover:border-border-strong focus:border-accent",
          "aria-invalid:border-danger",
          "disabled:cursor-not-allowed disabled:opacity-60",
          className,
        )}
        {...props}
      />
      {hint ? (
        <p id={hintId} className="text-xs leading-relaxed text-subtle">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p
          id={errorId}
          className="text-xs leading-relaxed text-danger"
          role="alert"
        >
          {error}
        </p>
      ) : null}
    </div>
  );
}
