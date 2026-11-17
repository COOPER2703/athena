import type { SettingField } from "../../api/settings";
import { Badge } from "../../shared/ui/Badge";
import { Button } from "../../shared/ui/Button";
import {
  BOOLEAN_CHOICES,
  canonicalValue,
  displayValue,
  inputModeFor,
  readOnlyReason,
  settingHint,
  settingLabel,
} from "./model";
import { SourceBadge } from "./SourceBadge";

const CONTROL_CLASS =
  "h-10 w-full rounded-sm border border-border bg-canvas px-3 font-mono text-sm text-text " +
  "transition-quiet hover:border-border-strong focus:border-accent " +
  "disabled:cursor-not-allowed disabled:opacity-60 aria-invalid:border-danger";

export interface SettingRowProps {
  field: SettingField;
  value: string;
  changed: boolean;
  error?: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  onReset: () => void;
}

/**
 * Une ligne de Réglage : valeur effective, source, contrôle et réinitialisation.
 *
 * Un champ en lecture seule n'a aucun contrôle : la Console affiche la valeur
 * que le processus utilise et explique pourquoi elle ne peut pas être écrite.
 * Aucun champ masqué par l'environnement n'est jamais envoyé au serveur.
 */
export function SettingRow({
  field,
  value,
  changed,
  error,
  disabled = false,
  onChange,
  onReset,
}: SettingRowProps) {
  const inputId = `setting-input-${field.key}`;
  const hintId = `setting-hint-${field.key}`;
  const errorId = `setting-error-${field.key}`;
  const writable = field.editable && !field.readOnly;
  const hint = settingHint(field);
  const describedBy =
    [writable && hint ? hintId : null, error ? errorId : null]
      .filter(Boolean)
      .join(" ") || undefined;
  const canReset =
    writable &&
    canonicalValue(field, value) !==
      canonicalValue(field, field.defaultValue);

  return (
    <div
      id={`setting-${field.key}`}
      className="grid gap-4 border-b border-border py-5 last:border-b-0 lg:grid-cols-[minmax(14rem,1fr)_minmax(0,1.15fr)]"
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {writable ? (
            <label
              htmlFor={inputId}
              className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-text"
            >
              {settingLabel(field.key)}
            </label>
          ) : (
            <p className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-text">
              {settingLabel(field.key)}
            </p>
          )}
          <SourceBadge source={field.source} />
          {field.pending ? <Badge tone="warning">En attente</Badge> : null}
          {!field.editable ? <Badge>Lecture seule</Badge> : null}
        </div>

        <p className="mt-2 break-all font-mono text-[0.625rem] uppercase tracking-[0.1em] text-subtle">
          {field.env}
        </p>

        {writable && hint ? (
          <p id={hintId} className="mt-2 text-xs leading-relaxed text-subtle">
            {hint}
          </p>
        ) : null}
        {!writable ? (
          <p className="mt-2 text-xs leading-relaxed text-subtle">
            {readOnlyReason(field)}
          </p>
        ) : null}
      </div>

      <div className="flex min-w-0 flex-col gap-2">
        {writable ? (
          renderControl(
            field,
            inputId,
            value,
            describedBy,
            disabled,
            error !== undefined,
            onChange,
          )
        ) : (
          <output
            id={inputId}
            className="flex min-h-10 items-center rounded-sm border border-dashed border-border bg-canvas/40 px-3 py-2 font-mono text-sm text-muted"
          >
            {displayValue(field)}
          </output>
        )}

        <p className="text-xs leading-relaxed text-muted">
          Valeur effective :{" "}
          <code className="font-mono text-text">{displayValue(field)}</code>
        </p>

        {changed && writable ? (
          <p className="text-xs leading-relaxed text-accent">
            Sera enregistré :{" "}
            <code className="font-mono">
              {displayValue({ ...field, value })}
            </code>
          </p>
        ) : null}
        {field.pending && !changed ? (
          <p className="text-xs leading-relaxed text-warning">
            Après redémarrage :{" "}
            <code className="font-mono">
              {displayValue({
                ...field,
                value: field.pendingValue ?? field.defaultValue,
              })}
            </code>
            {field.pendingValue === null ? " (retour au défaut)" : null}
          </p>
        ) : null}

        {error ? (
          <p id={errorId} role="alert" className="text-xs leading-relaxed text-danger">
            {error}
          </p>
        ) : null}

        {writable ? (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onReset}
              disabled={disabled || !canReset}
              title={`Revenir à la valeur par défaut : ${field.defaultValue}`}
              aria-label={`Réinitialiser ${settingLabel(field.key)}`}
            >
              Réinitialiser
            </Button>
            <span className="font-mono text-[0.625rem] text-subtle">
              défaut : {field.defaultValue === "" ? "—" : field.defaultValue}
            </span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function renderControl(
  field: SettingField,
  id: string,
  value: string,
  describedBy: string | undefined,
  disabled: boolean,
  invalid: boolean,
  onChange: (value: string) => void,
) {
  const common = {
    id,
    name: field.key,
    disabled,
    "aria-describedby": describedBy,
    "aria-invalid": invalid || undefined,
    className: CONTROL_CLASS,
  } as const;

  if (field.kind === "bool") {
    return (
      <select
        {...common}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      >
        {BOOLEAN_CHOICES.map((choice) => (
          <option key={choice.value} value={choice.value}>
            {choice.label}
          </option>
        ))}
      </select>
    );
  }

  if (field.choices.length > 0) {
    const options = field.choices.includes(value)
      ? field.choices
      : [value, ...field.choices];
    return (
      <select
        {...common}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      >
        {options.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </select>
    );
  }

  return (
    <input
      {...common}
      type="text"
      inputMode={inputModeFor(field.kind)}
      autoComplete="off"
      spellCheck={false}
      value={value}
      onChange={(event) => {
        onChange(event.target.value);
      }}
    />
  );
}
