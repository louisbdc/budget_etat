"""Postes peu pilotables à court terme (marquage visuel uniquement).

Règles volontairement simples et explicites ; à affiner une fois les libellés
réels connus. Aucune n'influence les calculs.
"""

from __future__ import annotations

import re

# Programme 117 « Charge de la dette et trésorerie de l'État ».
CHARGE_DETTE_PROGRAMMES = {"117"}

RULES = [
    # (côté, champ testé, regex, niveau, explication)
    ("depense", "programme_code", r"^117$", "fixe", "Charge de la dette : dépend du stock et des taux"),
    ("depense", "lib", r"charge de la dette", "fixe", "Charge de la dette : dépend du stock et des taux"),
    ("depense", "lib", r"pension", "partiel", "Pensions : droits acquis, ajustement lent"),
    ("depense", "titre_code", r"^2$", "partiel", "Titre 2 (personnel) : effectifs et grilles, inertie forte"),
    ("depense", "lib", r"personnel", "partiel", "Dépenses de personnel : inertie forte"),
    ("recette", "lib", r"union europ", "fixe", "Prélèvement UE : fixé par le cadre financier pluriannuel"),
]


def flag(side: str, **fields: str | None) -> dict | None:
    """Retourne {niveau, raison} pour le premier critère rencontré, sinon None."""
    for s, champ, rx, niveau, raison in RULES:
        if s != side:
            continue
        values = [fields.get(k) for k in fields if k.endswith("lib")] if champ == "lib" else [fields.get(champ)]
        if any(v and re.search(rx, str(v), re.I) for v in values):
            return {"niveau": niveau, "raison": raison}
    return None
