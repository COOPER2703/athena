import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  describeError,
  describeFieldErrors,
} from "../../api/errors";
import { queryKeys } from "../../api/queryClient";
import {
  fetchSettings,
  saveSettings,
  type SettingsPayload,
} from "../../api/settings";
import { useAuth } from "../../auth/AuthProvider";
import { Button } from "../../shared/ui/Button";
import { Notice } from "../../shared/ui/Notice";
import { AccessTokensPanel } from "./AccessTokensPanel";
import { AccountPanel } from "./AccountPanel";
import {
  changedKeys,
  discardChanges,
  draftFromFields,
  pendingFields,
  restartPlan,
  settingsPayload,
  settingsSections,
  settingsUnavailable,
  surfaceChanges,
  validateSettings,
  type SurfaceChange,
} from "./model";
import { PendingBanner } from "./PendingBanner";
import { SecretsPanel } from "./SecretsPanel";
import { SettingsSection } from "./SettingsSection";
import { clientTransportUrl } from "./tokens";
import { UrlConfirmDialog } from "./UrlConfirmDialog";
import { useRestartRecovery } from "./useRestartRecovery";

type Feedback = {
  tone: "success" | "danger" | "info";
  text: string;
};

interface Confirmation {
  values: Record<string, string>;
  changes: SurfaceChange[];
}

function browserOrigin(): string {
  return typeof window === "undefined" ? "" : window.location.origin;
}

function browserHostname(): string {
  return typeof window === "undefined" ? "" : window.location.hostname;
}

function transportUrl(settings: SettingsPayload | undefined): string | null {
  const surface = settings?.surfaces.find((item) => item.name === "transport");
  return clientTransportUrl(surface?.url ?? null, browserHostname());
}

/**
 * Zone Réglages : valeurs effectives, sources, brouillon, application,
 * redémarrage, Secrets et Jetons d'accès.
 *
 * Le serveur reste l'autorité : le formulaire valide en miroir pour éviter un
 * aller-retour évident, puis envoie l'objet **complet**. Un changement d'hôte
 * ou de port passe par une confirmation explicite de la nouvelle URL. Un
 * magasin indisponible est expliqué : la Console, les Secrets et les Jetons
 * restent utilisables.
 */
export function SettingsZone() {
  const queryClient = useQueryClient();
  const { session, refetch: refetchSession } = useAuth();
  const settings = useQuery({
    queryKey: queryKeys.settings,
    queryFn: () => fetchSettings(),
  });

  const [draft, setDraft] = useState<Record<string, string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [restarting, setRestarting] = useState(false);
  const [verifying, setVerifying] = useState(false);

  const signature =
    settings.data?.fields
      .map(
        (field) =>
          `${field.key}=${field.value}|${field.pending ? field.pendingValue ?? "" : "-"}|${field.readOnly}|${field.defaultValue}`,
      )
      .join("\u0000") ?? "";

  // Une lecture ou une écriture remplace les valeurs de référence : le
  // brouillon repart de l'état serveur, jamais d'une saisie périmée.
  useEffect(() => {
    if (settings.data === undefined) {
      return;
    }
    setDraft(draftFromFields(settings.data.fields));
    setFieldErrors({});
  }, [signature]);

  const recoveryPhase = useRestartRecovery(restarting, () => {
    setRestarting(false);
    setFeedback({
      tone: "success",
      text: "Le serveur a redémarré : la Console est reconnectée.",
    });
    void queryClient.invalidateQueries();
    void refetchSession();
  });

  const save = useMutation({
    mutationFn: (input: {
      values: Record<string, string>;
      confirmUrls?: string[];
    }) => saveSettings(input.values, input.confirmUrls),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKeys.settings, next);
      setFieldErrors({});
      setConfirmation(null);
      const plan = restartPlan(next, browserOrigin());
      setRestarting(plan.kind === "poll");
      setFeedback(feedbackFor(plan));
      // La session ne dit pas la même chose pendant un redémarrage : on la
      // relit seulement quand le serveur reste joignable. Le retour de santé
      // invalide tout le cache une fois le serveur revenu.
      if (plan.kind === "none" || plan.kind === "manual") {
        void refetchSession();
      }
    },
    onError: (error) => {
      const fields = describeFieldErrors(error);
      setFieldErrors(fields);
      setConfirmation(null);
      setFeedback({
        tone: "danger",
        text:
          fields.confirm_url ??
          (Object.keys(fields).length > 0
            ? "Certains réglages sont refusés : corrigez les champs signalés."
            : describeError(error)),
      });
      focusFirstField(fields);
    },
  });

  const data = settings.data;
  const sections = data === undefined ? [] : settingsSections(data.fields);
  const changes =
    data === undefined ? [] : changedKeys(data.fields, draft);
  const plan =
    data === undefined ? { kind: "none" as const } : restartPlan(data, browserOrigin());
  const pending = data === undefined ? [] : pendingFields(data);
  const unavailable = settings.isError && settingsUnavailable(settings.error);

  function updateField(key: string, value: string): void {
    setDraft((current) => ({ ...current, [key]: value }));
    setFieldErrors((current) => {
      if (current[key] === undefined) {
        return current;
      }
      const next = { ...current };
      delete next[key];
      return next;
    });
    setFeedback(null);
  }

  function resetField(key: string): void {
    if (data === undefined) {
      return;
    }
    setDraft((current) => {
      const field = data.fields.find((item) => item.key === key);
      if (field === undefined || !field.editable || field.readOnly) {
        return current;
      }
      return { ...current, [key]: field.defaultValue };
    });
    setFeedback(null);
  }

  function cancelChanges(): void {
    if (data === undefined) {
      return;
    }
    setDraft(discardChanges(data.fields));
    setFieldErrors({});
    setFeedback(null);
  }

  function beginSave(): void {
    if (data === undefined || save.isPending) {
      return;
    }
    const errors = validateSettings(data.fields, draft);
    if (Object.keys(errors).length > 0) {
      setFieldErrors(errors);
      setFeedback({
        tone: "danger",
        text: "Corrigez les champs signalés, puis réessayez.",
      });
      focusFirstField(errors);
      return;
    }
    const values = settingsPayload(data.fields, draft);
    const moved = surfaceChanges(data, draft);
    setFeedback(null);
    setFieldErrors({});
    if (moved.length > 0) {
      setConfirmation({ values, changes: moved });
      return;
    }
    save.mutate({ values });
  }

  function confirmSave(): void {
    if (confirmation === null) {
      return;
    }
    save.mutate({
      values: confirmation.values,
      confirmUrls: confirmation.changes.map((change) => change.nextUrl),
    });
  }

  async function verifyRestart(): Promise<void> {
    if (verifying) {
      return;
    }
    setVerifying(true);
    setFeedback(null);
    try {
      const result = await settings.refetch();
      if (result.error !== null) {
        throw result.error;
      }
      await refetchSession();
      if (result.data !== undefined && !result.data.restart.required) {
        setRestarting(false);
        setFeedback({
          tone: "success",
          text: "Le serveur a redémarré : les changements sont actifs.",
        });
      } else {
        setFeedback({
          tone: "info",
          text: "Le serveur répond encore avec l'ancienne configuration : le redémarrage n'a pas encore eu lieu.",
        });
      }
    } catch (error) {
      setFeedback({ tone: "danger", text: describeError(error) });
    } finally {
      setVerifying(false);
    }
  }

  return (
    <div
      className="flex flex-col gap-6 px-5 py-8 sm:px-8 lg:px-10 lg:py-10"
      aria-busy={save.isPending}
    >
      {feedback !== null ? (
        <Notice
          tone={
            feedback.tone === "success"
              ? "success"
              : feedback.tone === "info"
                ? "info"
                : "danger"
          }
        >
          <span role={feedback.tone === "danger" ? "alert" : "status"}>
            {feedback.text}
          </span>
        </Notice>
      ) : null}

      {data !== undefined ? (
        <PendingBanner
          payload={data}
          plan={plan}
          phase={recoveryPhase}
          verifying={verifying}
          onVerify={() => {
            void verifyRestart();
          }}
        />
      ) : null}

      {changes.length > 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-3 border border-accent-line bg-accent-soft px-4 py-3">
          <p className="text-sm text-text">
            {changes.length} modification{changes.length > 1 ? "s" : ""} non
            enregistrée{changes.length > 1 ? "s" : ""}.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="primary"
              size="sm"
              onClick={beginSave}
              disabled={save.isPending}
              aria-busy={save.isPending}
            >
              {save.isPending ? "Enregistrement…" : "Enregistrer"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={cancelChanges}
              disabled={save.isPending}
            >
              Annuler les modifications
            </Button>
          </div>
        </div>
      ) : null}

      {settings.isPending ? (
        <Notice tone="info" title="Lecture des réglages">
          La Console interroge la configuration effective du serveur…
        </Notice>
      ) : null}

      {unavailable ? (
        <Notice tone="warning" title="Réglages indisponibles">
          <p>{describeError(settings.error)}</p>
          <p className="mt-2 text-xs">
            La Conversation, le Diagnostic, les Secrets et les Jetons restent
            utilisables. Aucun réglage n'a été modifié.
          </p>
          <Button
            variant="secondary"
            size="sm"
            className="mt-3"
            onClick={() => {
              void settings.refetch();
            }}
          >
            Réessayer
          </Button>
        </Notice>
      ) : null}

      {settings.isError && !unavailable ? (
        <Notice tone="danger" title="Réglages illisibles">
          <p>{describeError(settings.error)}</p>
          <Button
            variant="secondary"
            size="sm"
            className="mt-3"
            onClick={() => {
              void settings.refetch();
            }}
          >
            Réessayer
          </Button>
        </Notice>
      ) : null}

      {data !== undefined
        ? sections.map((section) => (
            <SettingsSection
              key={section.id}
              section={section}
              draft={draft}
              changed={changes}
              errors={fieldErrors}
              disabled={save.isPending}
              onChange={updateField}
              onReset={resetField}
            />
          ))
        : null}

      {data !== undefined && pending.length === 0 && plan.kind === "none" ? (
        <p className="text-xs leading-relaxed text-subtle">
          Les valeurs affichées sont celles que le serveur utilise maintenant.
        </p>
      ) : null}

      <SecretsPanel
        unreadable={
          session?.secretsUnreadable === true ||
          data?.secretsUnreadable === true
        }
      />
      <AccessTokensPanel transportUrl={transportUrl(data)} />
      <AccountPanel />

      <UrlConfirmDialog
        open={confirmation !== null}
        changes={confirmation?.changes ?? []}
        busy={save.isPending}
        onCancel={() => {
          setConfirmation(null);
        }}
        onConfirm={confirmSave}
      />
    </div>
  );
}

function feedbackFor(plan: ReturnType<typeof restartPlan>): Feedback {
  switch (plan.kind) {
    case "none":
      return { tone: "success", text: "Réglages enregistrés." };
    case "manual":
      return {
        tone: "success",
        text: "Réglages enregistrés : un redémarrage reste nécessaire.",
      };
    case "moved":
      return {
        tone: "info",
        text: "Réglages enregistrés : la Console répondra à une nouvelle adresse après le redémarrage.",
      };
    case "poll":
      return {
        tone: "info",
        text: "Réglages enregistrés : le serveur redémarre…",
      };
  }
}

function focusFirstField(errors: Readonly<Record<string, string>>): void {
  const first = Object.keys(errors).find((key) => key !== "confirm_url");
  if (first === undefined) {
    return;
  }
  window.setTimeout(() => {
    const element = document.getElementById(`setting-input-${first}`);
    if (element instanceof HTMLElement) {
      element.focus();
    }
  }, 0);
}
