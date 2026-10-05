"""Programmes (politiques ou personnels) : listes de mesures sourcées, rejouées
dans le simulateur.

Un programme est un fichier JSON de data/programmes/ (format dans le README).
Règles :
- chaque mesure chiffrée par un tiers ou par le porteur cite sa source (URL +
  citation) ; une mesure sans source n'est admise que si `chiffrage` vaut
  « utilisateur » (hypothèse personnelle), et elle est signalée comme telle ;
- les cibles sont résolues contre l'année de base du simulateur ; une cible
  introuvable est signalée, jamais ignorée en silence ;
- tous les programmes sont projetés avec les MÊMES hypothèses macro (celles de
  l'interface) : les écarts entre programmes ne viennent que des mesures.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from budget_etat.fetch import ROOT

PROGRAMMES_DIR = ROOT / "data" / "programmes"
COTES = {"depense": "spending", "recette": "revenue"}
CHIFFRAGES = {"porteur", "tiers", "utilisateur"}


class ProgrammeInvalide(ValueError):
    pass


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def valider(data: dict, ident: str) -> dict:
    """Contrôle la structure ; renvoie le programme normalisé ou lève ProgrammeInvalide."""
    erreurs = []
    for champ in ("nom", "porteur", "mesures"):
        if champ not in data:
            erreurs.append(f"champ « {champ} » manquant")
    mesures = data.get("mesures") or []
    for i, m in enumerate(mesures, 1):
        p = f"mesure {i} ({m.get('libelle', '?')})"
        if m.get("cote") not in COTES:
            erreurs.append(f"{p} : « cote » doit valoir depense ou recette")
        if m.get("mode") not in ("pct", "meur"):
            erreurs.append(f"{p} : « mode » doit valoir pct ou meur")
        if not isinstance(m.get("valeur"), (int, float)):
            erreurs.append(f"{p} : « valeur » numérique obligatoire (aucun chiffre n'est deviné)")
        if not isinstance(m.get("debut"), int):
            erreurs.append(f"{p} : « debut » (année) obligatoire")
        if not m.get("cible"):
            erreurs.append(f"{p} : « cible » obligatoire")
        if m.get("chiffrage") not in CHIFFRAGES:
            erreurs.append(f"{p} : « chiffrage » doit valoir porteur, tiers ou utilisateur")
        src = m.get("source") or {}
        if m.get("chiffrage") in ("porteur", "tiers") and not (src.get("url") and src.get("citation")):
            erreurs.append(f"{p} : chiffrage « {m.get('chiffrage')} » sans source (url + citation)")
    if erreurs:
        raise ProgrammeInvalide(f"{ident} : " + " ; ".join(erreurs))
    return {**data, "id": ident, "mesures": mesures}


def lister(directory: Path = PROGRAMMES_DIR) -> list[dict]:
    """Résumé de chaque programme, valide ou non (l'erreur est renvoyée)."""
    out = []
    for f in sorted(directory.glob("*.json")) if directory.exists() else []:
        if f.name.startswith("_"):
            continue  # modèles
        try:
            p = charger(f.stem, directory)
            out.append({"id": p["id"], "nom": p["nom"], "porteur": p["porteur"], "nb_mesures": len(p["mesures"]),
                        "sans_source": sum(1 for m in p["mesures"] if m["chiffrage"] == "utilisateur"),
                        "statut": p.get("statut"), "erreur": None})
        except (ProgrammeInvalide, json.JSONDecodeError) as e:
            out.append({"id": f.stem, "nom": f.stem, "porteur": None, "nb_mesures": 0, "sans_source": 0,
                        "statut": None, "erreur": str(e)})
    return out


def charger(ident: str, directory: Path = PROGRAMMES_DIR) -> dict:
    if not re.fullmatch(r"[\w\-]{1,80}", ident):
        raise ProgrammeInvalide("identifiant de programme invalide")
    f = directory / f"{ident}.json"
    if not f.exists():
        raise FileNotFoundError(ident)
    return valider(json.loads(f.read_text(encoding="utf-8")), ident)


def resoudre(programme: dict, base: dict) -> list[dict]:
    """Associe chaque mesure à une ligne ou un groupe de l'année de base.

    Cibles admises : « P:146 » (programme), « M:Défense » (mission, libellé
    comparé sans accents ni casse), « R:IR » (poste de recette), « C:fiscale »
    (catégorie de recettes), « * » (toutes les lignes du côté visé).
    Renvoie les mesures au format du simulateur, avec `erreur` si non résolue.
    """
    lignes = {"spending": base.get("spending", []), "revenue": base.get("revenue", [])}
    out = []
    for m in programme["mesures"]:
        side = COTES[m["cote"]]
        cible = str(m["cible"]).strip()
        keys = {l["key"] for l in lignes[side]}
        groups = {l["group"]: l["group_label"] for l in lignes[side]}
        target, erreur = None, None
        if cible == "*" or cible in keys or cible in groups:
            target = cible
        elif cible[:2] in ("M:", "C:"):
            voulu = _plain(cible[2:])
            trouves = [g for g, lab in groups.items() if g[:2] == cible[:2] and _plain(lab) == voulu] or \
                      [g for g, lab in groups.items() if g[:2] == cible[:2] and voulu and voulu in _plain(lab)]
            if len(trouves) == 1:
                target = trouves[0]
            else:
                erreur = (f"« {cible} » introuvable dans l'année de base" if not trouves else
                          f"« {cible} » ambigu : {', '.join(groups[g] for g in trouves)}")
        else:
            erreur = f"« {cible} » introuvable dans l'année de base"
        out.append({
            "side": side, "target": target, "mode": m["mode"], "value": m["valeur"], "start_year": m["debut"],
            "ramp_years": int(m.get("montee_en_charge") or 1), "libelle": m.get("libelle"),
            "chiffrage": m["chiffrage"], "source": m.get("source"), "note": m.get("note"), "erreur": erreur,
            "cible": cible,
        })
    return out
