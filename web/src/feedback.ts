/** Retour d'action affiché sous un formulaire : succès, erreur, ou rien. */
export type Feedback =
  | { kind: "success"; message: string }
  | { kind: "error"; message: string }
  | null;
