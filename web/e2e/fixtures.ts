import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer, type AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { test as base } from "@playwright/test";

/**
 * Fixtures des parcours navigateur (ticket #179).
 *
 * Chaque parcours démarre **son** serveur de test : répertoire temporaire,
 * ports réservés par l'OS, aucun secret réel. Le serveur Python
 * (`e2e/server.py`) est un superviseur : il relance le serveur du seam partagé
 * à la même adresse après chaque sortie propre, ce qui rend le redémarrage
 * observable par un navigateur.
 */

export type ServerState = "fresh" | "installed";

export interface E2EServer {
  /** Base HTTP de la Console servie par le serveur de test. */
  readonly url: string;
  /** Petit serveur de contrôle du superviseur (jamais exposé par l'app). */
  readonly controlUrl: string;
  /** Génération courante : elle change à chaque redémarrage effectif. */
  generation(): Promise<number>;
  /** Demande une sortie propre et attend la génération suivante. */
  restart(): Promise<number>;
  /**
   * Fait tenir une Session à un Client desktop du Protocole partagé.
   *
   * Aucune Console ne peut alors démarrer : le noyau est occupé par un autre
   * Client, et la voix refuse sans préempter.
   */
  occupySession(): Promise<void>;
  /** Libère la Session du Client desktop et attend le retour au Repos. */
  releaseSession(): Promise<void>;
}

interface Fixtures {
  server: E2EServer;
}

interface Options {
  /** `fresh` : aucun Compte ni clé ; `installed` : Compte semé et clé factice. */
  serverState: ServerState;
}

const REPO_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
);
const READY_TIMEOUT_MS = 30_000;
const EXIT_TIMEOUT_MS = 5_000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/**
 * Réserve `count` ports distincts en gardant les sockets ouverts jusqu'au bout.
 *
 * Deux réservations séparées peuvent rendre le même port éphémère ; les
 * surfaces du serveur de test entreraient alors en collision.
 */
async function freePorts(count: number): Promise<number[]> {
  const servers = await Promise.all(
    Array.from({ length: count }, () => {
      return new Promise<ReturnType<typeof createServer>>((resolve, reject) => {
        const server = createServer();
        server.once("error", reject);
        server.listen(0, "127.0.0.1", () => {
          resolve(server);
        });
      });
    }),
  );
  const ports = servers.map((server) => (server.address() as AddressInfo).port);
  await Promise.all(
    servers.map(
      (server) =>
        new Promise<void>((resolve) => {
          server.close(() => {
            resolve();
          });
        }),
    ),
  );
  return ports;
}

interface ControlState {
  generation: number;
  ready: boolean;
  url: string;
}

async function readControl(controlUrl: string): Promise<ControlState> {
  const response = await fetch(`${controlUrl}/state`);
  if (!response.ok) {
    throw new Error(`contrôle E2E : HTTP ${response.status}`);
  }
  return (await response.json()) as ControlState;
}

async function waitFor(
  controlUrl: string,
  predicate: (state: ControlState) => boolean,
  output: () => string,
): Promise<ControlState> {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const state = await readControl(controlUrl);
      if (predicate(state)) {
        return state;
      }
    } catch {
      // Le serveur de contrôle peut être momentanément injoignable au boot.
    }
    await sleep(100);
  }
  throw new Error(
    `le serveur E2E n'a pas répondu comme attendu :\n${output()}`,
  );
}

async function stopChild(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  const exited = new Promise<void>((resolve) => {
    child.once("exit", () => {
      resolve();
    });
  });
  child.kill("SIGTERM");
  const timedOut = await Promise.race([
    exited.then(() => false),
    sleep(EXIT_TIMEOUT_MS).then(() => true),
  ]);
  if (timedOut) {
    child.kill("SIGKILL");
    await exited;
  }
}

export const test = base.extend<Fixtures & Options>({
  serverState: ["installed", { option: true }],

  server: async ({ serverState }, use) => {
    const root = await mkdtemp(path.join(tmpdir(), "athena-e2e-"));
    const [port, transportPort, controlPort] = await freePorts(3);
    const python = process.env.ATHENA_E2E_PYTHON ?? "python";
    const child = spawn(
      python,
      [
        path.join(REPO_ROOT, "e2e", "server.py"),
        "--root",
        root,
        "--port",
        String(port),
        "--transport-port",
        String(transportPort),
        "--control-port",
        String(controlPort),
        "--state",
        serverState,
        "--static-dir",
        path.join(REPO_ROOT, "web", "dist"),
      ],
      {
        cwd: REPO_ROOT,
        // Le drapeau est celui du déploiement : le serveur sort proprement et
        // le superviseur le relance à la même adresse.
        env: { ...process.env, ATHENA_RESTART_ON_APPLY: "1" },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );

    let output = "";
    const collect = (chunk: Buffer): void => {
      output += chunk.toString();
    };
    child.stdout?.on("data", collect);
    child.stderr?.on("data", collect);

    const controlUrl = `http://127.0.0.1:${controlPort}`;
    const server: E2EServer = {
      url: `http://127.0.0.1:${port}`,
      controlUrl,
      async generation(): Promise<number> {
        const state = await readControl(controlUrl);
        return state.generation;
      },
      async restart(): Promise<number> {
        const before = await server.generation();
        const response = await fetch(`${controlUrl}/restart`, {
          method: "POST",
        });
        if (!response.ok) {
          throw new Error(`redémarrage E2E refusé : HTTP ${response.status}`);
        }
        const state = await waitFor(
          controlUrl,
          (current) => current.ready && current.generation > before,
          () => output,
        );
        return state.generation;
      },
      async occupySession(): Promise<void> {
        const response = await fetch(`${controlUrl}/session/occupy`, {
          method: "POST",
        });
        if (!response.ok) {
          throw new Error(
            `Session E2E non occupée : HTTP ${response.status}\n${output}`,
          );
        }
      },
      async releaseSession(): Promise<void> {
        const response = await fetch(`${controlUrl}/session/release`, {
          method: "POST",
        });
        if (!response.ok) {
          throw new Error(
            `Session E2E non libérée : HTTP ${response.status}\n${output}`,
          );
        }
      },
    };

    try {
      await waitFor(controlUrl, (state) => state.ready, () => output);
      await use(server);
    } finally {
      await stopChild(child);
      await rm(root, { recursive: true, force: true });
    }
  },
});

export { expect } from "@playwright/test";
