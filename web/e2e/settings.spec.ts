import { E2E_PASSWORD, E2E_USERNAME } from "./constants";
import { expect, test } from "./fixtures";

/**
 * Parcours 3 — un réglage modifié, appliqué, et la Console revenue après
 * redémarrage.
 *
 * Le réglage choisi est la voix : non masquée par l'environnement, sans effet
 * sur l'adresse de la Console (contrairement à un hôte ou un port). Le
 * parcours vérifie le bandeau « changements en attente », la sortie propre du
 * serveur, la relance par le superviseur, puis la valeur effective après
 * redémarrage, avec sa source « Enregistré ».
 */

test.use({ serverState: "installed" });

test("un réglage appliqué survit au redémarrage", async ({ page, server }) => {
  await page.goto(server.url);
  await page.getByLabel("Nom du Compte").fill(E2E_USERNAME);
  await page.getByLabel("Mot de passe").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Se connecter" }).click();
  await expect(page).toHaveURL(/\/conversation$/);

  await page.goto(`${server.url}/settings`);
  const voice = page.locator("#setting-input-voice");
  await expect(voice).toBeVisible();
  await expect(page.getByText("Les valeurs affichées sont celles que le serveur utilise maintenant.")).toBeVisible();

  const initial = await voice.inputValue();
  const next = initial === "Puck" ? "Leda" : "Puck";
  await voice.fill(next);
  await expect(
    page.getByText("1 modification non enregistrée."),
  ).toBeVisible();

  const generation = await server.generation();
  // La barre de brouillon est le premier « Enregistrer » de la page ; celle
  // des Secrets, plus bas, porte le même libellé pour un champ vide.
  await page
    .getByRole("button", { name: "Enregistrer", exact: true })
    .first()
    .click();

  // --- Bandeau des changements en attente --------------------------------
  await expect(
    page.getByText("1 changement, redémarrage requis"),
  ).toBeVisible();
  await expect(page.locator("#setting-voice")).toContainText("En attente");

  // --- Redémarrage effectif et retour de la Console ----------------------
  await expect(
    page.getByText("Le serveur a redémarré : la Console est reconnectée."),
  ).toBeVisible({ timeout: 60_000 });

  await expect.poll(() => server.generation()).toBeGreaterThan(generation);

  // --- La valeur est active, et plus rien n'attend ----------------------
  await page.reload();
  const reloaded = page.locator("#setting-input-voice");
  await expect(reloaded).toHaveValue(next);
  await expect(page.locator("#setting-voice")).toContainText("Enregistré");
  await expect(
    page.getByText("1 changement, redémarrage requis"),
  ).toHaveCount(0);
  await expect(
    page.getByText("Les valeurs affichées sont celles que le serveur utilise maintenant."),
  ).toBeVisible();
});
