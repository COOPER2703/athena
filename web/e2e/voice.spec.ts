import type { Page } from "@playwright/test";

import { audioStarts, installAudioProbe } from "./audio";
import {
  E2E_PASSWORD,
  E2E_USERNAME,
  E2E_VOICE_BUSY,
  E2E_VOICE_HEARD,
  E2E_VOICE_REPLY,
} from "./constants";
import { expect, test, type E2EServer } from "./fixtures";

/**
 * Parcours 4 — conversation vocale avec micro simulé.
 *
 * Chromium capture un fichier WAV (`client/tests/data/athena.wav`) à la place
 * d'un vrai périphérique : le parcours ne dépend d'aucun micro et aucune
 * permission n'est demandée à la personne qui lance la CI. Le faux modèle
 * reconnaît un énoncé déterministe, le transcrit avant de répondre, puis rend
 * une réponse texte et audio.
 *
 * Le parcours prouve d'abord le refus sans préemption — un Client desktop tient
 * déjà la Session —, puis le chemin complet : « Connexion… » avant « Parlez »,
 * les mots de la personne dans le fil en direct, la réponse d'Athena, et la
 * croix qui rend la barre de saisie.
 */

declare global {
  interface Window {
    __athenaVoiceProbe?: {
      statuses: string[];
      liveLines: string[];
    };
  }
}

/**
 * Sonde d'interface vocale : elle enregistre ce qui a été **réellement** rendu.
 *
 * « Connexion… » peut disparaître avant le premier contrôle Playwright ; une
 * sonde branchée avant le chargement de la page observe la séquence sans
 * ajouter de délai arbitraire. Elle retient aussi les lignes du fil marquées
 * « en direct », que la relecture du Journal remplace ensuite.
 */
async function installVoiceProbe(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const statuses: string[] = [];
    const liveLines = new Set<string>();
    const probe = { statuses, liveLines: [] as string[] };
    Object.defineProperty(window, "__athenaVoiceProbe", {
      value: probe,
      configurable: true,
    });

    const recordStatus = (): void => {
      const bar = document.querySelector(
        'section[aria-label="Conversation vocale"]',
      );
      const status = bar?.querySelector('[role="status"]');
      const text = status?.textContent?.trim() ?? "";
      if (text !== "" && statuses[statuses.length - 1] !== text) {
        statuses.push(text);
      }
    };

    const recordLiveArticles = (node: Node): void => {
      if (!(node instanceof Element)) {
        return;
      }
      const articles = node.matches("article")
        ? [node]
        : Array.from(node.querySelectorAll("article"));
      for (const article of articles) {
        const text = article.textContent?.trim() ?? "";
        if (text.includes("en direct")) {
          liveLines.add(text);
        }
      }
    };

    // On observe le document lui-même : à l'exécution du script d'init,
    // `document.documentElement` peut ne pas exister encore.
    new MutationObserver((records) => {
      recordStatus();
      for (const record of records) {
        for (const node of record.addedNodes) {
          recordLiveArticles(node);
        }
      }
      probe.liveLines = Array.from(liveLines);
    }).observe(document, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  });
}

async function voiceProbe(
  page: Page,
): Promise<{ statuses: string[]; liveLines: string[] }> {
  return page.evaluate(() => {
    return window.__athenaVoiceProbe ?? { statuses: [], liveLines: [] };
  });
}

async function signIn(page: Page, server: E2EServer): Promise<void> {
  await page.goto(server.url);
  await page.getByLabel("Nom du Compte").fill(E2E_USERNAME);
  await page.getByLabel("Mot de passe").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Se connecter" }).click();
  await expect(page).toHaveURL(/\/conversation$/);
}

test.use({ serverState: "installed", permissions: ["microphone"] });

test("refus sans préemption, puis conversation vocale simulée", async ({
  page,
  server,
}) => {
  await installAudioProbe(page);
  await installVoiceProbe(page);
  await signIn(page, server);

  const voiceBar = page.getByRole("region", {
    name: "Conversation vocale",
  });

  // --- Refus : un Client desktop tient déjà la Session -------------------
  await server.occupySession();
  await page.getByRole("button", { name: "Parler" }).click();

  const refused = page
    .getByRole("alert")
    .filter({ hasText: "Conversation vocale indisponible" });
  await expect(
    refused,
    "la Console doit refuser plutôt que préempter la Session du desktop",
  ).toBeVisible();
  await expect(refused).toContainText(E2E_VOICE_BUSY);
  await expect(page.getByLabel("Message")).toBeVisible();
  await expect(voiceBar).toHaveCount(0);
  // Le desktop n'a pas été préempté : la Session en cours n'est pas celle de
  // la Console (`session.console` reste faux).
  const during = (await (
    await page.request.get(`${server.url}/api/status`)
  ).json()) as { session: { state: string; console: boolean } };
  expect(during.session.state).not.toBe("Idle");
  expect(during.session.console).toBe(false);
  await refused.getByRole("button", { name: "Fermer" }).click();

  // Le desktop est libéré, et le noyau revient au Repos : le départ suivant
  // ne court pas après une Session en cours de fermeture.
  await server.releaseSession();

  // --- Départ : « Connexion… » avant « Parlez » --------------------------
  await page.getByRole("button", { name: "Parler" }).click();
  await expect(
    voiceBar.getByText("Parlez", { exact: true }),
    "la trame ready doit ouvrir « Parlez » : sans elle, le micro ne s'ouvre jamais",
  ).toBeVisible({ timeout: 20_000 });

  const connected = await voiceProbe(page);
  expect(
    connected.statuses,
    "les états visibles doivent montrer la connexion puis l'écoute",
  ).toContain("Connexion…");
  expect(connected.statuses.indexOf("Connexion…")).toBeLessThan(
    connected.statuses.indexOf("Parlez"),
  );

  // --- Les mots de la personne apparaissent, en direct -------------------
  await expect(
    page.getByText(E2E_VOICE_HEARD, { exact: true }).first(),
    "le faux modèle doit avoir entendu l'audio du micro simulé",
  ).toBeVisible({ timeout: 20_000 });

  // --- Réponse d'Athena : texte, état et audio ---------------------------
  await expect(
    page.getByText(E2E_VOICE_REPLY, { exact: true }).first(),
    "la réponse du faux modèle doit arriver dans le fil",
  ).toBeVisible();
  await expect(voiceBar.getByText("Athena répond", { exact: true })).toBeVisible();
  await expect.poll(() => audioStarts(page)).toBeGreaterThan(0);

  const live = await voiceProbe(page);
  expect(
    live.liveLines.some(
      (line) => line.includes(E2E_VOICE_HEARD) && line.includes("en direct"),
    ),
    "la parole de la personne doit être passée par une ligne « en direct »",
  ).toBe(true);

  // --- La croix termine et rend la barre de saisie -----------------------
  await voiceBar.getByRole("button", { name: "Terminer" }).click();
  await expect(page.getByLabel("Message")).toBeVisible();
  await expect(voiceBar).toHaveCount(0);
  await expect(
    page.getByText("Vous avez terminé la conversation."),
  ).toBeVisible();

  // Le Journal reprend les deux lignes, chacune une seule fois.
  await expect(page.getByText(E2E_VOICE_HEARD, { exact: true })).toHaveCount(1);
  await expect(page.getByText(E2E_VOICE_REPLY, { exact: true })).toHaveCount(1);
  await expect(page.getByText("Voix", { exact: true }).first()).toBeVisible();
});
