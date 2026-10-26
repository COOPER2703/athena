import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Le serveur de développement ne parle qu'à la Console locale. Le port suit la
// même variable que le serveur (`ATHENA_CONSOLE_PORT`) pour que les deux
// restent alignés sans seconde configuration.
const consolePort = process.env.ATHENA_CONSOLE_PORT ?? "8767";
const consoleTarget = `http://127.0.0.1:${consolePort}`;

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Une seule entrée : l'API REST et le WebSocket de traces vivent tous
      // deux sous `/api`, et `ws` couvre `/api/stream`.
      "/api": {
        target: consoleTarget,
        ws: true,
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
