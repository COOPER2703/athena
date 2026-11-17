import { MoonIcon, SunIcon } from "../shared/icons";
import { Button } from "../shared/ui/Button";
import { useTheme } from "./ThemeProvider";

/** Bascule sombre/clair ; le libellé annonce l'action, pas l'état courant. */
export function ThemeToggle() {
  const { theme, label, toggle } = useTheme();
  const next = theme === "dark" ? "clair" : "sombre";

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={toggle}
      aria-label={`Passer au thème ${next}`}
      title={`Passer au thème ${next}`}
      icon={theme === "dark" ? <SunIcon /> : <MoonIcon />}
    >
      <span className="hidden sm:inline">{label}</span>
    </Button>
  );
}
