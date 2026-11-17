import { BrandMark } from "../../shared/icons";

/** Attente de `GET /api/session` : la Console ne montre rien avant de savoir. */
export function SplashScreen() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-5 bg-canvas px-6">
      <BrandMark className="h-10 w-10 text-accent" />
      <p
        role="status"
        className="font-mono text-xs uppercase tracking-[0.22em] text-muted"
      >
        Chargement de la Console…
      </p>
    </div>
  );
}
