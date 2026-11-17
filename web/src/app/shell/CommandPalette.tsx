import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { cn } from "../../shared/cn";
import { CloseIcon, SearchIcon } from "../../shared/icons";
import { Kbd } from "../../shared/ui/Kbd";
import { filterCommands, moveSelection, type Command } from "./palette";

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  commands: readonly Command[];
}

/**
 * Palette de commandes : une seule liste, filtrable, entièrement au clavier.
 *
 * Elle ne connaît ni les modules ni le routeur : chaque commande porte son
 * action, la coquille décide de ce qu'elle propose.
 */
export function CommandPalette({
  open,
  onClose,
  commands,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  const filtered = useMemo(
    () => filterCommands(query, commands),
    [commands, query],
  );

  useEffect(() => {
    if (!open) {
      return;
    }
    restoreFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setQuery("");
    setSelected(0);
    const frame = window.requestAnimationFrame(() => {
      inputRef.current?.focus();
    });
    return () => {
      window.cancelAnimationFrame(frame);
      restoreFocusRef.current?.focus();
    };
  }, [open]);

  useEffect(() => {
    setSelected(0);
  }, [query]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const root = document.documentElement;
    const previous = root.style.overflow;
    root.style.overflow = "hidden";
    return () => {
      root.style.overflow = previous;
    };
  }, [open]);

  useEffect(() => {
    if (!open || selected < 0) {
      return;
    }
    const option = document.getElementById(
      `palette-option-${filtered[selected]?.id ?? ""}`,
    );
    option?.scrollIntoView({ block: "nearest" });
  }, [filtered, open, selected]);

  if (!open) {
    return null;
  }

  function run(command: Command | undefined): void {
    if (!command) {
      return;
    }
    onClose();
    command.run();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    switch (event.key) {
      case "Escape":
        event.preventDefault();
        onClose();
        return;
      case "Tab":
        // Une seule zone de saisie : la tabulation ne sort pas de la palette.
        event.preventDefault();
        inputRef.current?.focus();
        return;
      case "ArrowDown":
        event.preventDefault();
        setSelected((current) => moveSelection(current, 1, filtered.length));
        return;
      case "ArrowUp":
        event.preventDefault();
        setSelected((current) => moveSelection(current, -1, filtered.length));
        return;
      case "Home":
        event.preventDefault();
        setSelected(filtered.length > 0 ? 0 : -1);
        return;
      case "End":
        event.preventDefault();
        setSelected(filtered.length - 1);
        return;
      case "Enter":
        event.preventDefault();
        run(filtered[selected]);
        return;
      default:
        return;
    }
  }

  let lastGroup: string | null = null;
  const active = filtered[selected];

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-canvas/90 px-4 pt-[12vh]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="palette-title"
        className="w-full max-w-xl overflow-hidden rounded-sm border border-border bg-surface shadow-[0_30px_80px_-40px_rgba(0,0,0,0.9)]"
        onKeyDown={handleKeyDown}
      >
        <h2 id="palette-title" className="sr-only">
          Palette de commandes
        </h2>
        <div className="flex items-center gap-3 border-b border-border px-4">
          <SearchIcon className="h-4 w-4 shrink-0 text-subtle" />
          <input
            ref={inputRef}
            role="combobox"
            aria-label="Rechercher une commande"
            aria-autocomplete="list"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={
              active ? `palette-option-${active.id}` : undefined
            }
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Rechercher une zone ou une action…"
            className="h-12 w-full bg-transparent font-mono text-sm text-text outline-none placeholder:text-subtle"
          />
          <button
            type="button"
            onClick={onClose}
            aria-label="Fermer la palette"
            className="rounded-sm p-1 text-subtle transition-quiet hover:bg-surface-2 hover:text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            <CloseIcon className="h-4 w-4" />
          </button>
        </div>

        <div
          id="palette-list"
          role="listbox"
          aria-label="Commandes"
          className="max-h-[min(24rem,50vh)] overflow-y-auto p-2"
        >
          {filtered.length === 0 ? (
            <p role="status" className="px-3 py-6 text-sm text-muted">
              Aucune commande ne correspond.
            </p>
          ) : (
            filtered.map((command, index) => {
              const showGroup = command.group !== lastGroup;
              lastGroup = command.group;
              return (
                <Fragment key={command.id}>
                  {showGroup ? (
                    <p
                      role="presentation"
                      className="px-3 pb-1 pt-3 font-mono text-[0.625rem] uppercase tracking-[0.18em] text-subtle"
                    >
                      {command.group}
                    </p>
                  ) : null}
                  <div
                    id={`palette-option-${command.id}`}
                    role="option"
                    aria-selected={index === selected}
                    tabIndex={-1}
                    onMouseMove={() => setSelected(index)}
                    onClick={() => run(command)}
                    className={cn(
                      "flex cursor-pointer items-center justify-between gap-3 rounded-sm border-l-2 px-3 py-2 text-sm transition-quiet",
                      index === selected
                        ? "border-accent bg-accent-soft text-text"
                        : "border-transparent text-muted hover:bg-surface-2",
                    )}
                  >
                    <span>{command.label}</span>
                    {command.shortcut ? (
                      <Kbd className="shrink-0">{command.shortcut}</Kbd>
                    ) : null}
                  </div>
                </Fragment>
              );
            })
          )}
        </div>

        <div className="flex flex-wrap items-center gap-3 border-t border-border px-4 py-2.5 font-mono text-[0.625rem] text-subtle">
          <span className="flex items-center gap-1">
            <Kbd>↑</Kbd>
            <Kbd>↓</Kbd>
            naviguer
          </span>
          <span className="flex items-center gap-1">
            <Kbd>↵</Kbd>
            exécuter
          </span>
          <span className="flex items-center gap-1">
            <Kbd>esc</Kbd>
            fermer
          </span>
        </div>
      </div>
    </div>
  );
}
