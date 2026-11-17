import {
  E2E_GEMINI_KEY,
  E2E_MESSAGE,
  E2E_PASSWORD,
  E2E_REPLY,
  E2E_USERNAME,
} from "./constants";
import { expect, test } from "./fixtures";

/**
 * Parcours 1 — Installation jusqu'à la Conversation utilisable.
 *
 * Instance neuve : aucun Compte, aucune clé. Le premier visiteur crée l'unique
 * Compte, note la clé maître affichée une seule fois, enregistre une clé Gemini
 * factice (validée par le faux validateur, sans réseau), termine le parcours.
 * La clé enregistrée n'étant active qu'après un redémarrage, le superviseur
 * relance le serveur ; la Console revient, la Conversation s'ouvre et le
 * premier message reçoit une réponse du faux modèle.
 */

test.use({ serverState: "fresh" });

test("installation neuve puis première réponse de la Conversation", async ({
  page,
  server,
}) => {
  await page.goto(server.url);

  // --- Compte ------------------------------------------------------------
  await expect(
    page.getByRole("heading", { name: "Créer le Compte" }),
  ).toBeVisible();
  await page.getByLabel("Nom du Compte").fill(E2E_USERNAME);
  await page.getByLabel("Mot de passe", { exact: true }).fill(E2E_PASSWORD);
  await page
    .getByLabel("Confirmation du mot de passe")
    .fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Créer le Compte" }).click();

  // --- Clé maître, affichée une fois et confirmée ------------------------
  await expect(
    page.getByRole("heading", { name: "Votre clé maître" }),
  ).toBeVisible();
  const masterKey = page.locator('pre[aria-label="Clé maître"]');
  await expect(masterKey).toBeVisible();
  const masterKeyText = (await masterKey.textContent())?.trim() ?? "";
  expect(masterKeyText.length).toBeGreaterThan(20);
  await expect(
    page.getByText("Elle ne sera plus jamais affichée, ni ici, ni ailleurs."),
  ).toBeVisible();

  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Continuer vers la clé Gemini" })
    .click();

  // La clé ne survit pas à l'étape qui l'affiche.
  await expect(masterKey).toHaveCount(0);
  await page.reload();
  await expect(masterKey).toHaveCount(0);

  // --- Clé Gemini factice, validée par le faux validateur ----------------
  await expect(
    page.getByRole("heading", { name: "Clé Gemini" }),
  ).toBeVisible();
  await page.getByLabel("Clé Gemini").fill(E2E_GEMINI_KEY);
  await page.getByRole("button", { name: "Enregistrer la clé" }).click();

  // --- Voix et profil, puis récapitulatif --------------------------------
  await expect(
    page.getByRole("heading", { name: "Voix et profil" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continuer" }).click();

  await expect(
    page.getByRole("heading", { name: "Récapitulatif" }),
  ).toBeVisible();
  await expect(page.getByText("Redémarrage requis")).toBeVisible();

  // La clé est enregistrée mais pas encore chargée : la Conversation reste
  // fermée et le rappel permanent de l'Installation le dit.
  await page.goto(`${server.url}/conversation`);
  await expect(page.getByText("Conversation désactivée")).toBeVisible();
  await expect(
    page.getByRole("status", { name: "Installation incomplète" }),
  ).toBeVisible();

  // --- Redémarrage du superviseur, puis Console revenue -----------------
  const generation = await server.restart();
  expect(generation).toBeGreaterThan(0);

  await page.goto(`${server.url}/conversation`);
  await expect(
    page.getByRole("heading", { name: "Conversation", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Message")).toBeVisible();
  await expect(
    page.getByRole("status", { name: "Installation incomplète" }),
  ).toHaveCount(0);

  // --- Premier message : texte du faux modèle ---------------------------
  await page.getByLabel("Message").fill(E2E_MESSAGE);
  await page.getByRole("button", { name: "Envoyer" }).click();

  await expect(page.getByText(E2E_REPLY).first()).toBeVisible();
  await expect(page.getByText("Clavier").first()).toBeVisible();
  await expect(page.getByText(E2E_MESSAGE).first()).toBeVisible();
});
