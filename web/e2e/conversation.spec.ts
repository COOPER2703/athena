import { audioStarts, installAudioProbe } from "./audio";
import {
  E2E_MESSAGE,
  E2E_PASSWORD,
  E2E_REPLY,
  E2E_SLOW_MESSAGE,
  E2E_SLOW_REPLY,
  E2E_USERNAME,
} from "./constants";
import { expect, test } from "./fixtures";

/**
 * Parcours 2 — connexion, un Tour complet avec texte et audio, puis
 * interruption.
 *
 * L'instance est déjà installée et une clé factice est chargée au démarrage :
 * le faux modèle répond aux messages. Le parcours prouve le cookie de
 * connexion, le rendu du flux SSE (texte, Tool, activité), la lecture réelle de
 * l'audio après le geste de sourdine, puis l'annulation d'un Tour lent : le
 * message envoyé reste visible, le bouton redevient « Envoyer » et le noyau
 * revient au Repos.
 */

test.use({ serverState: "installed" });

test("connexion, Tour texte et audio, puis interruption", async ({
  page,
  server,
}) => {
  await installAudioProbe(page);
  await page.goto(server.url);

  // --- Connexion ---------------------------------------------------------
  await expect(
    page.getByRole("heading", { name: "Connexion" }),
  ).toBeVisible();
  await page.getByLabel("Nom du Compte").fill(E2E_USERNAME);
  await page.getByLabel("Mot de passe").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Se connecter" }).click();

  await expect(page).toHaveURL(/\/conversation$/);
  await expect(page.getByLabel("Message")).toBeVisible();

  // --- Le son est coupé par défaut et s'active au geste ------------------
  const muted = page.getByRole("button", { name: "Activer le son" });
  await expect(muted).toBeVisible();
  await muted.click();
  await expect(
    page.getByRole("button", { name: "Couper le son" }),
  ).toBeVisible();

  // --- Tour ordinaire : Tool, activité, texte, audio --------------------
  await page.getByLabel("Message").fill(E2E_MESSAGE);
  await page.getByRole("button", { name: "Envoyer" }).click();

  // Le Tool est exécuté avant la réponse finale : le panneau le montre.
  await expect(page.getByText("save_memory")).toBeVisible();
  await expect(page.getByText("Athena répond…")).toBeVisible();
  await expect(page.getByText(E2E_REPLY).first()).toBeVisible();
  await expect(page.getByText("Clavier").first()).toBeVisible();

  // L'audio a été réellement démarré par le lecteur de la Conversation.
  await expect.poll(() => audioStarts(page)).toBeGreaterThan(0);

  // --- Tour lent, interrompu --------------------------------------------
  await page.getByLabel("Message").fill(E2E_SLOW_MESSAGE);
  await page.getByRole("button", { name: "Envoyer" }).click();

  const stop = page.getByRole("button", { name: "Interrompre" });
  await expect(stop).toBeVisible();
  await stop.click();

  await expect(page.getByText("Tour interrompu")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Envoyer" }),
  ).toBeVisible();
  // La réponse lente n'est jamais arrivée, et le message envoyé reste lisible.
  await expect(page.getByText(E2E_SLOW_REPLY)).toHaveCount(0);
  await expect(page.getByText(E2E_SLOW_MESSAGE).first()).toBeVisible();

  // Le noyau est bien revenu au Repos, pas resté sur le Tour interrompu.
  await expect
    .poll(async () => {
      const response = await page.request.get(`${server.url}/api/status`);
      const snapshot = (await response.json()) as {
        session: { state: string };
      };
      return snapshot.session.state;
    })
    .toBe("Idle");
});

/**
 * Le fil s'ouvre déjà défilé sur son message le plus récent : arriver sur le
 * plus ancien de la fenêtre donnerait l'impression d'un historique figé. Le
 * Journal est simulé par interception, sans faire vivre soixante Tours au faux
 * modèle.
 */
test("le fil s'ouvre sur son message le plus récent", async ({
  page,
  server,
}) => {
  await page.goto(server.url);
  await page.getByLabel("Nom du Compte").fill(E2E_USERNAME);
  await page.getByLabel("Mot de passe").fill(E2E_PASSWORD);
  await page.getByRole("button", { name: "Se connecter" }).click();
  await expect(page).toHaveURL(/\/conversation$/);

  const entries = Array.from({ length: 60 }, (_, index) => {
    const rank = String(index + 1).padStart(3, "0");
    return {
      id: index + 1,
      role: index % 2 === 0 ? "user" : "assistant",
      origin: "text",
      text: `Repère du fil ${rank}`,
      created_at: new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString(),
    };
  });
  await page.route("**/api/thread*", (route) =>
    route.fulfill({
      json: { v: 1, entries, next_before: null, retention_days: 3 },
    }),
  );

  await page.reload();

  await expect(page.getByText("Repère du fil 060")).toBeInViewport();
  await expect(page.getByText("Repère du fil 001")).not.toBeInViewport();
});
