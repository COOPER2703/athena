import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./app/App";
import { applyTheme, initialTheme, THEME_STORAGE_KEY } from "./design/theme";
import "./styles/index.css";

// Le thème est posé avant le premier rendu : pas de flash clair sur un écran
// sombre, même quand le choix vient du stockage local.
try {
  applyTheme(
    initialTheme(window.localStorage.getItem(THEME_STORAGE_KEY)),
    document.documentElement,
  );
} catch {
  applyTheme(initialTheme(null), document.documentElement);
}

const container = document.getElementById("root");
if (container === null) {
  throw new Error("Élément #root introuvable dans index.html");
}

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
