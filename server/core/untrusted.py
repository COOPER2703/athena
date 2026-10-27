"""Rendu du contenu externe destiné à un modèle : une donnée, jamais un cadre.

Le contenu venu du Canal d'événements ne doit pouvoir ni fabriquer une ligne
supplémentaire, ni simuler un en-tête, ni refermer le bloc de données dans
lequel un prompt l'insère (#81). Cette règle est la même pour le bloc de faits
du noyau et pour celui du juge ; seuls les délimiteurs et les en-têtes diffèrent,
et restent donc des paramètres.

Module **pur** : aucune I/O, aucun état. Les appelants gardent leur propre forme
brute (Journal, dédoublonnage du Rappel) et n'utilisent ceci que pour le texte
exposé au modèle.
"""

from __future__ import annotations

from typing import Pattern


def flatten_untrusted(text: str) -> str:
    """Aplatit un contenu sur une seule ligne : un fait reste un fait."""
    return " ".join(text.splitlines())


def sanitize_untrusted(
    text: str,
    *,
    forbidden: Pattern[str],
    replacement: str,
) -> str:
    """Aplatit ``text`` puis neutralise tout délimiteur ou en-tête forgeable.

    ``forbidden`` porte le motif propre au bloc visé et ``replacement`` le
    marqueur lisible qui le remplace : les deux appelants gardent leurs
    conventions, mais une seule règle décide qu'un contenu externe ne peut pas
    parler depuis la position du prompt.
    """
    return forbidden.sub(replacement, flatten_untrusted(text))
