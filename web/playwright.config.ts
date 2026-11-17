import { existsSync } from "node:fs";

import { chromium, defineConfig } from "@playwright/test";

/**
 * Parcours navigateur de la Console (ticket #179).
 *
 * Les tests tournent sur le build réel (`web/dist`) servi par le serveur de
 * test, jamais sur le serveur de développement Vite. Chaque parcours démarre
 * son propre serveur isolé (voir `e2e/fixtures.ts`), donc l'ordre d'exécution
 * n'a aucune importance et aucun secret réel n'est requis.
 *
 * Chromium, par ordre de préférence :
 * 1. `ATHENA_E2E_CHROMIUM` (chemin explicite) ;
 * 2. le binaire téléchargé par Playwright (`npx playwright install chromium`) ;
 * 3. un Chromium système (`/usr/bin/chromium`, Chrome), pour qu'un poste déjà
 *    équipé n'ait rien à télécharger.
 */

const SYSTEM_CANDIDATES = [
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  "/usr/bin/google-chrome",
];

function bundledChromium(): string | undefined {
  try {
    const executable = chromium.executablePath();
    return existsSync(executable) ? executable : undefined;
  } catch {
    return undefined;
  }
}

function chromiumExecutable(): string | undefined {
  const explicit = process.env.ATHENA_E2E_CHROMIUM;
  if (explicit !== undefined && explicit !== "") {
    return explicit;
  }
  return (
    bundledChromium() ??
    SYSTEM_CANDIDATES.find((candidate) => existsSync(candidate))
  );
}

const executablePath = chromiumExecutable();

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  // Trois parcours, trois serveurs : deux workers suffisent sans saturer la CI.
  workers: process.env.CI ? 2 : undefined,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      ...(executablePath === undefined ? {} : { executablePath }),
      // Le parcours 2 doit entendre la réponse après le geste de sourdine.
      args: ["--autoplay-policy=no-user-gesture-required"],
    },
  },
});
