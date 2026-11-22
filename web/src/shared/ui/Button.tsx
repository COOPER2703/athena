import type { ButtonHTMLAttributes, ReactNode } from "react";

import { cn } from "../cn";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "square";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
}

const VARIANTS: Readonly<Record<ButtonVariant, string>> = {
  primary:
    "border-accent-line bg-accent-fill text-accent-ink hover:bg-accent-fill-hover",
  secondary:
    "border-border bg-surface text-text hover:border-border-strong hover:bg-surface-2",
  ghost:
    "border-transparent bg-transparent text-muted hover:bg-surface-2 hover:text-text",
  danger: "border-danger-line bg-transparent text-danger hover:bg-danger-soft",
};

const SIZES: Readonly<Record<ButtonSize, string>> = {
  sm: "h-8 gap-1.5 px-2.5 text-xs",
  md: "h-10 gap-2 px-4 text-sm",
  square: "h-9 w-9 p-0",
};

/** Bouton de la Console : quatre variantes, trois tailles, focus toujours visible. */
export function Button({
  variant = "secondary",
  size = "md",
  icon,
  className,
  type = "button",
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cn(
        "inline-flex items-center justify-center rounded-sm border font-medium transition-quiet",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:opacity-50",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...props}
    >
      {icon}
      {children}
    </button>
  );
}
