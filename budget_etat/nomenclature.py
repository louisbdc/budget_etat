"""Rapprochement de la nomenclature mission / programme d'une année à l'autre.

Règles, par ordre de priorité :
1. `manuel` : correspondance déclarée dans data/correspondances.csv
   (colonnes : exercice, programme_code, programme_canon, mission_canon, note).
   Une ligne avec exercice vide s'applique à toutes les années.
2. `code+libelle` : même numéro de programme qu'à l'exercice de référence et
   libellé proche (similarité de Jaccard sur les mots ≥ SEUIL).
3. `code_seul` : même numéro mais libellé différent -> rapproché, mais signalé
   (un numéro peut être réutilisé pour un autre périmètre).
4. `non_rapproche` : numéro absent de l'exercice de référence.

Le canon d'un programme est son code à l'exercice de référence (le plus récent) ;
la mission canonique est la mission de rattachement à cet exercice.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SEUIL = 0.5
_STOP = {"de", "des", "du", "la", "le", "les", "et", "l", "d", "a", "aux", "au", "en", "pour", "sur"}


def normalise(lib: str | None) -> set[str]:
    if not lib:
        return set()
    s = unicodedata.normalize("NFKD", lib).encode("ascii", "ignore").decode().lower()
    return {w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _STOP}


def similarity(a: str | None, b: str | None) -> float:
    x, y = normalise(a), normalise(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


@dataclass(frozen=True)
class Programme:
    exercice: int
    code: str
    lib: str | None
    mission_lib: str | None


@dataclass(frozen=True)
class Match:
    exercice: int
    programme_code: str
    programme_lib: str | None
    mission_lib: str | None
    programme_canon: str | None
    mission_canon: str | None
    methode: str


def load_manual(path: Path) -> dict[tuple[int | None, str], tuple[str, str | None]]:
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ex = int(row["exercice"]) if row.get("exercice") else None
            out[(ex, row["programme_code"].strip())] = (row["programme_canon"].strip(), row.get("mission_canon") or None)
    return out


def reconcile(programmes: list[Programme], manual: dict | None = None, reference: int | None = None) -> list[Match]:
    manual = manual or {}
    if not programmes:
        return []
    ref_year = reference or max(p.exercice for p in programmes)
    ref = {p.code: p for p in programmes if p.exercice == ref_year}
    out = []
    for p in programmes:
        key = (p.exercice, p.code) if (p.exercice, p.code) in manual else (None, p.code)
        if key in manual:
            canon, mission = manual[key]
            if mission is None and canon in ref:
                mission = ref[canon].mission_lib
            out.append(Match(p.exercice, p.code, p.lib, p.mission_lib, canon, mission, "manuel"))
        elif p.code in ref:
            r = ref[p.code]
            methode = "code+libelle" if p.exercice == ref_year or similarity(p.lib, r.lib) >= SEUIL else "code_seul"
            out.append(Match(p.exercice, p.code, p.lib, p.mission_lib, r.code, r.mission_lib, methode))
        else:
            out.append(Match(p.exercice, p.code, p.lib, p.mission_lib, None, None, "non_rapproche"))
    return out
