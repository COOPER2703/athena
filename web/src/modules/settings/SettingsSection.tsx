import { useId } from "react";

import type { SettingsSection as SettingsSectionModel } from "./model";
import type { SettingField } from "../../api/settings";
import { baselineValue } from "./model";
import { SettingRow } from "./SettingRow";

export interface SettingsSectionProps {
  section: SettingsSectionModel;
  draft: Readonly<Record<string, string>>;
  changed: readonly string[];
  errors: Readonly<Record<string, string>>;
  disabled?: boolean;
  onChange: (key: string, value: string) => void;
  onReset: (key: string) => void;
}

/** Une section de Réglages : un titre, un filet, puis les champs. */
export function SettingsSection({
  section,
  draft,
  changed,
  errors,
  disabled = false,
  onChange,
  onReset,
}: SettingsSectionProps) {
  const titleId = useId();

  return (
    <section
      aria-labelledby={titleId}
      className="border border-border bg-surface"
    >
      <header className="border-b border-border px-4 py-3 sm:px-5">
        <h2
          id={titleId}
          className="font-mono text-[0.6875rem] uppercase tracking-[0.14em] text-muted"
        >
          {section.title}
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-subtle">
          {section.summary}
        </p>
      </header>
      <div className="px-4 sm:px-5">
        {section.fields.map((field) => (
          <SettingRow
            key={field.key}
            field={field}
            value={valueOf(field, draft)}
            changed={changed.includes(field.key)}
            error={errors[field.key]}
            disabled={disabled}
            onChange={(value) => {
              onChange(field.key, value);
            }}
            onReset={() => {
              onReset(field.key);
            }}
          />
        ))}
      </div>
    </section>
  );
}

function valueOf(
  field: SettingField,
  draft: Readonly<Record<string, string>>,
): string {
  return draft[field.key] ?? baselineValue(field);
}
