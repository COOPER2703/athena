import { QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { createQueryClient } from "../api/queryClient";
import { AuthProvider, useAuth } from "../auth/AuthProvider";
import { LoginScreen } from "../auth/LoginScreen";
import { ThemeProvider } from "../design/ThemeProvider";
import { ConversationRoute } from "../installation/ConversationLocked";
import { InstallationScreen } from "../installation/InstallationScreen";
import { VoiceProvider } from "../modules/conversation/VoiceProvider";
import { DiagnosticZone } from "../modules/diagnostic/DiagnosticZone";
import { PluginsZone } from "../modules/plugins/PluginsZone";
import { SettingsZone } from "../modules/settings/SettingsZone";
import { IdentityGate } from "./IdentityGate";
import { NotFound } from "./NotFound";
import { RequireApp } from "./RequireApp";
import { defaultZone, screenFor } from "./routing";
import { AppShell } from "./shell/AppShell";

/**
 * La conversation vocale ne vit que sous un Ticket valide : une déconnexion
 * démonte le fournisseur, donc coupe le micro et la lecture. Le fournisseur
 * reste au-dessus du routeur pour survivre aux changements de zone.
 */
function VoiceShell({ children }: { children: ReactNode }) {
  const { session } = useAuth();
  // Le fournisseur reste monté pendant l'Installation et la connexion : le
  // remplacer par un fragment remonterait tout le routeur et perdrait l'étape
  // en cours. Il libère simplement ses ressources quand le Ticket n'est plus
  // valide.
  return (
    <VoiceProvider active={screenFor(session) === "app"}>
      {children}
    </VoiceProvider>
  );
}

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
            <VoiceShell>
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
            </VoiceShell>
          </IdentityGate>
        </AuthProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
