import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { createQueryClient } from "../api/queryClient";
import { AuthProvider } from "../auth/AuthProvider";
import { LoginScreen } from "../auth/LoginScreen";
import { ThemeProvider } from "../design/ThemeProvider";
import { ConversationRoute } from "../installation/ConversationLocked";
import { InstallationScreen } from "../installation/InstallationScreen";
import { DiagnosticZone } from "../modules/diagnostic/DiagnosticZone";
import { PluginsZone } from "../modules/plugins/PluginsZone";
import { SettingsZone } from "../modules/settings/SettingsZone";
import { IdentityGate } from "./IdentityGate";
import { NotFound } from "./NotFound";
import { RequireApp } from "./RequireApp";
import { defaultZone } from "./routing";
import { AppShell } from "./shell/AppShell";

/**
 * Racine de la Console : état serveur, thème, identité, puis routeur.
 *
 * Les modules sont montés sous la coquille ; chacun possède sa route et son
 * état vide, prêt à être rempli sans toucher à cette arborescence.
 */
export function App() {
  const [queryClient] = useState(createQueryClient);

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AuthProvider>
          <IdentityGate>
            <Routes>
              <Route path="/login" element={<LoginScreen />} />
              <Route path="/installation" element={<InstallationScreen />} />
              <Route element={<RequireApp />}>
                <Route element={<AppShell />}>
                  <Route
                    index
                    element={<Navigate to={defaultZone().path} replace />}
                  />
                  <Route path="conversation" element={<ConversationRoute />} />
                  <Route path="settings/*" element={<SettingsZone />} />
                  <Route path="plugins/*" element={<PluginsZone />} />
                  <Route path="diagnostic/*" element={<DiagnosticZone />} />
                  <Route path="*" element={<NotFound />} />
                </Route>
              </Route>
            </Routes>
          </IdentityGate>
        </AuthProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
