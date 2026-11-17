import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { AuthLayout } from "../auth/AuthLayout";
import { useAuth } from "../auth/AuthProvider";
import { defaultZone } from "../app/routing";
import { cn } from "../shared/cn";
import { AccountStep } from "./AccountStep";
import { GeminiStep } from "./GeminiStep";
import { MasterKeyStep } from "./MasterKeyStep";
import { SummaryStep } from "./SummaryStep";
import { VoiceStep } from "./VoiceStep";
import {
  INSTALL_STEPS,
  nextStep,
  startStep,
  stepLabel,
  stepPosition,
  type InstallStep,
} from "./installation";

interface StepCopy {
  title: string;
  summary: string;
}

function copyFor(step: InstallStep): StepCopy {
  switch (step) {
    case "account":
      return {
        title: "Créer le Compte",
        summary:
          "Aucun Compte n'existe sur cette instance. Le premier visiteur devient propriétaire ; aucun Code d'installation n'est demandé.",
      };
    case "master-key":
      return {
        title: "Votre clé maître",
        summary:
          "Elle chiffre les Secrets au repos. Elle est créée maintenant et ne sera plus jamais affichée.",
      };
    case "gemini":
      return {
        title: "Clé Gemini",
        summary:
          "Elle permet à Athena de parler. Cette étape est passable : l'Installation pourra être terminée plus tard depuis les Réglages.",
      };
    case "voice":
      return {
        title: "Voix et profil",
        summary:
          "Un aperçu de ce avec quoi Athena démarrera. Ces valeurs se règlent ensuite dans les Réglages.",
      };
    case "summary":
      return {
        title: "Récapitulatif",
        summary:
          "Ce qui reste à faire pour qu'Athena soit pleinement opérationnelle.",
      };
  }
}

/** Fil d'Ariane des étapes : quatre jalons, la clé maître comptant pour le Compte. */
function StepTrail({ step }: { step: InstallStep }) {
  const current = stepPosition(step);
  return (
    <ol aria-label="Étapes de l'installation" className="mb-6 flex flex-wrap gap-x-4 gap-y-2">
      {INSTALL_STEPS.map((item, index) => {
        const number = index + 1;
        const active = number === current;
        const reached = number < current;
        return (
          <li
            key={item}
            aria-current={active ? "step" : undefined}
            className={cn(
              "flex items-center gap-1.5 font-mono text-[0.625rem] uppercase tracking-[0.12em]",
              active ? "text-accent" : reached ? "text-muted" : "text-subtle",
            )}
          >
            <span aria-hidden="true">{reached ? "✓" : `0${number}`}</span>
            {stepLabel(item)}
          </li>
        );
      })}
    </ol>
  );
}

/**
 * Parcours d'Installation, sans terminal ni Code.
 *
 * Compte, clé maître affichée une fois, clé Gemini passable, aperçu de la voix
 * et du profil, puis récapitulatif. Un Compte déjà créé reprend à la clé
 * Gemini ; une Installation terminée ne rouvre jamais cet écran.
 */
export function InstallationScreen() {
  const { session } = useAuth();
  const navigate = useNavigate();
  const [step, setStep] = useState<InstallStep | null>(() =>
    session === undefined ? null : startStep(session),
  );
  const [masterKey, setMasterKey] = useState<string | null>(null);

  if (step === null) {
    // Installation déjà terminée : l'écran guidé n'a plus lieu d'être.
    return <Navigate to={defaultZone().path} replace />;
  }

  const copy = copyFor(step);
  const position = stepPosition(step);
  const eyebrow = `Installation · 0${position} / 0${INSTALL_STEPS.length}`;

  function advance() {
    if (step === null) {
      return;
    }
    const next = nextStep(step);
    if (next === null) {
      navigate(defaultZone().path, { replace: true });
      return;
    }
    if (step === "master-key") {
      // La clé maître ne survit pas à l'étape qui l'affiche.
      setMasterKey(null);
    }
    setStep(next);
  }

  return (
    <AuthLayout
      eyebrow={eyebrow}
      title={copy.title}
      summary={copy.summary}
      footer="Un seul Compte par instance. Une fois l'Installation terminée, cet écran ne réapparaît plus."
    >
      <StepTrail step={step} />

      {step === "account" ? (
        <AccountStep
          minLength={session?.passwordMinLength ?? null}
          onInstalled={(key) => {
            setMasterKey(key);
            setStep("master-key");
          }}
        />
      ) : null}

      {step === "master-key" ? (
        <MasterKeyStep masterKey={masterKey} onContinue={advance} />
      ) : null}

      {step === "gemini" ? <GeminiStep onDone={advance} /> : null}

      {step === "voice" ? (
        <VoiceStep username={session?.username ?? null} onDone={advance} />
      ) : null}

      {step === "summary" ? <SummaryStep onFinish={advance} /> : null}
    </AuthLayout>
  );
}
