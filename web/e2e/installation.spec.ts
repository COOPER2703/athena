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
 * factice (validée par le faux validateur, sans réseau), voit les valeurs
 * effectives de voix, puis termine le parcours. La première clé active les
 * surfaces **sans redémarrage** : la Conversation s'ouvre immédiatement et le
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

  // --- Voix et profil : valeurs effectives, puis récapitulatif -----------
  await expect(
    page.getByRole("heading", { name: "Voix et profil" }),
  ).toBeVisible();
  // Les valeurs viennent de `GET /api/settings`, jamais d'une constante du front.
  const voice = page.locator("#installation-voice");
  await expect(voice).toBeVisible();
  await expect(voice).toHaveValue("Leda");
  await page.getByRole("button", { name: "Passer cette étape" }).click();

  await expect(
    page.getByRole("heading", { name: "Récapitulatif" }),
  ).toBeVisible();
  // La première clé a été activée à chaud : rien n'attend de redémarrage.
  await expect(
    page.getByText("Installation terminée", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Redémarrage requis")).toHaveCount(0);

  // --- La Conversation est ouverte immédiatement, sans redémarrage -------
  await page.getByRole("button", { name: "Ouvrir la Conversation" }).click();
  await expect(page).toHaveURL(/\/conversation$/);
  await expect(
    page.getByRole("heading", { name: "Conversation", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Message")).toBeVisible();
  await expect(
    page.getByRole("status", { name: "Installation incomplète" }),
  ).toHaveCount(0);

  // Aucune relance du superviseur n'a eu lieu.
  expect(await server.generation()).toBe(0);

  // --- Premier message : texte du faux modèle ---------------------------
  await page.getByLabel("Message").fill(E2E_MESSAGE);
  await page.getByRole("button", { name: "Envoyer" }).click();

  await expect(page.getByText(E2E_REPLY).first()).toBeVisible();
  await expect(page.getByText("Clavier").first()).toBeVisible();
  await expect(page.getByText(E2E_MESSAGE).first()).toBeVisible();
});
