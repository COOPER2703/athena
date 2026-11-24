/**
 * Icônes de la coquille : traits fins, `currentColor`, aucun jeu externe.
 *
 * Elles sont décoratives par défaut (`aria-hidden`) : le libellé voisin porte
 * le sens, jamais l'icône seule.
 */

export type IconProps = {
  className?: string;
};

const BASE = "h-4 w-4";

function svgProps(className?: string) {
  return {
    className: className ?? BASE,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    focusable: false,
  };
}

export function ConversationIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H9l-4 4v-4.5A2.5 2.5 0 0 1 4 13.5Z" />
      <path d="M8 9h8M8 12h5" />
    </svg>
  );
}

export function SettingsIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M4 7h10M18 7h2M4 12h3M11 12h9M4 17h7M15 17h5" />
      <circle cx="16" cy="7" r="2" />
      <circle cx="9" cy="12" r="2" />
      <circle cx="13" cy="17" r="2" />
    </svg>
  );
}

export function PluginsIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M10 4H7a2 2 0 0 0-2 2v3M14 4h3a2 2 0 0 1 2 2v3M20 14v3a2 2 0 0 1-2 2h-3M10 20H7a2 2 0 0 1-2-2v-3" />
      <circle cx="12" cy="12" r="2.5" />
    </svg>
  );
}

export function DiagnosticIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M3 12h4l2-5 3 10 2-5h7" />
    </svg>
  );
}

export function SearchIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <circle cx="11" cy="11" r="6" />
      <path d="m16 16 4 4" />
    </svg>
  );
}

export function SunIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4" />
    </svg>
  );
}

export function MoonIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" />
    </svg>
  );
}

export function LogOutIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M15 5h3a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-3" />
      <path d="M10 8l-4 4 4 4M6 12h10" />
    </svg>
  );
}

export function ArrowRightIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

export function CloseIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  );
}

export function RefreshIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M20 11a8 8 0 1 0-2.3 6.1" />
      <path d="M20 5v6h-6" />
    </svg>
  );
}

export function ShieldIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M12 3 5 6v5c0 4.5 3 7.8 7 10 4-2.2 7-5.5 7-10V6Z" />
      <path d="m9.5 12 1.8 1.8L15 10" />
    </svg>
  );
}

export function BrandMark({ className }: IconProps) {
  return (
    <svg
      className={className ?? "h-6 w-6"}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <circle
        cx="12"
        cy="12"
        r="8.5"
        stroke="currentColor"
        strokeWidth="1.2"
        opacity="0.45"
      />
      <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"
        stroke="currentColor"
        strokeWidth="1.2"
      />
      <circle cx="12" cy="12" r="1.4" fill="currentColor" />
    </svg>
  );
}

export function MicrophoneIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M9 21h6" />
    </svg>
  );
}

export function SendIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M4 12 20 5l-3.5 14L12 13l-8-1Z" />
      <path d="M12 13l3.5-6" />
    </svg>
  );
}

export function StopIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <rect x="7" y="7" width="10" height="10" rx="1.5" />
    </svg>
  );
}

export function SoundOnIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M4 10v4h3l4 3.5v-11L7 10H4Z" />
      <path d="M15 9.5a3.5 3.5 0 0 1 0 5M17.5 7a7 7 0 0 1 0 10" />
    </svg>
  );
}

export function SoundOffIcon({ className }: IconProps) {
  return (
    <svg {...svgProps(className)}>
      <path d="M4 10v4h3l4 3.5v-11L7 10H4Z" />
      <path d="m15 9.5 5 5M20 9.5l-5 5" />
    </svg>
  );
}
