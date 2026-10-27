import { useState, type FormEvent } from "react";

import {
  DEFAULT_SOURCE,
  MAX_CONTENT_CHARS,
  characterCount,
  submitDraft,
} from "./events";
import "./event-form.css";

/** Retour d'envoi affiché sous le formulaire. */
type Feedback =
  | { kind: "success"; message: string }
  | { kind: "error"; message: string }
  | null;

/** Formulaire d'injection d'un Événement dans le Canal d'événements. */
export function EventForm({
  onUnauthorized,
}: {
  /** Appelé quand l'injection est refusée par un 401 (Ticket expiré). */
  onUnauthorized?: () => void;
} = {}) {
  const [source, setSource] = useState(DEFAULT_SOURCE);
  const [content, setContent] = useState("");
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [pending, setPending] = useState(false);

  const count = characterCount(content);
  const overLimit = count > MAX_CONTENT_CHARS;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) {
      return;
    }
    setFeedback(null);
    setPending(true);
    try {
      const result = await submitDraft({ source, content });
      if (result.ok) {
        setFeedback({ kind: "success", message: result.message });
        setContent("");
      } else if (result.unauthorized === true) {
        onUnauthorized?.();
      } else {
        setFeedback({ kind: "error", message: result.message });
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel" aria-labelledby="event-form-title">
      <div className="panel__header">
        <h2 className="panel__title" id="event-form-title">
          Injection d'un Événement
        </h2>
      </div>

      <form className="event-form" onSubmit={handleSubmit}>
        <div className="event-form__fields">
          <label className="field field--source" htmlFor="event-source">
            <span className="field__label">Source</span>
            <input
              className="field__input mono"
              id="event-source"
              type="text"
              value={source}
              onChange={(event) => {
                setSource(event.target.value);
                setFeedback(null);
              }}
              autoComplete="off"
              spellCheck={false}
              disabled={pending}
            />
          </label>

          <label className="field" htmlFor="event-content">
            <span className="field__label">Contenu</span>
            <textarea
              className="field__input"
              id="event-content"
              rows={3}
              value={content}
              onChange={(event) => {
                setContent(event.target.value);
                setFeedback(null);
              }}
              placeholder="Ex. : porte ouverte"
              disabled={pending}
            />
          </label>
        </div>

        <div className="event-form__actions">
          <span
            className={
              overLimit
                ? "event-form__count event-form__count--over"
                : "event-form__count"
            }
            aria-live="polite"
          >
            {count} / {MAX_CONTENT_CHARS} caractères
          </span>
          <button className="button" type="submit" disabled={pending}>
            {pending ? "Envoi…" : "Injecter l'Événement"}
          </button>
        </div>

        {feedback !== null ? (
          <p
            className={
              feedback.kind === "success"
                ? "notice notice--success"
                : "notice notice--error"
            }
            role={feedback.kind === "error" ? "alert" : "status"}
          >
            {feedback.message}
          </p>
        ) : null}
      </form>
    </section>
  );
}
