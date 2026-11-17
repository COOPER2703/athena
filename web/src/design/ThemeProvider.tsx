import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  THEME_STORAGE_KEY,
  applyTheme,
  initialTheme,
  nextTheme,
  themeLabel,
  type Theme,
} from "./theme";

interface ThemeContextValue {
  theme: Theme;
  label: string;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function storedTheme(): string | null {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Applique et persiste le thème ; le sombre reste la valeur de départ. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => initialTheme(storedTheme()));

  useEffect(() => {
    applyTheme(theme, document.documentElement);
    const meta = document.querySelector<HTMLMetaElement>(
      'meta[name="theme-color"]',
    );
    if (meta) {
      meta.content = theme === "dark" ? "#0a0d10" : "#f5f7f5";
    }
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // Stockage refusé : le thème vaut pour l'onglet courant seulement.
    }
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => nextTheme(current));
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, label: themeLabel(theme), toggle }),
    [theme, toggle],
  );

  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (value === null) {
    throw new Error("useTheme doit être utilisé sous ThemeProvider.");
  }
  return value;
}
