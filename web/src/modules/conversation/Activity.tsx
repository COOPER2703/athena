/** Indicateur d'activité d'un Tour : texte seul, sans animation indispensable. */
export function Activity() {
  return (
    <p
      role="status"
      className="flex items-center gap-2 font-mono text-xs text-muted"
    >
      <span aria-hidden="true" className="flex items-center gap-1">
        {[0, 1, 2].map((index) => (
          <span
            key={index}
            className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent"
            style={{ animationDelay: `${index * 140}ms` }}
          />
        ))}
      </span>
      Athena répond…
    </p>
  );
}
