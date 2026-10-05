"""Parseurs des fichiers sources, écrits d'après data/INSPECTION.md.

Chaque parseur prend un chemin et produit des tuples (table, ligne) pour la base
canonique (montants convertis en M€). Le format est reconnu par les EN-TÊTES et
non par le nom de fichier : un fichier d'une autre année au même format est donc
pris en charge, et un format inconnu lève `UnknownFormat` (signalé, jamais deviné).

Formats observés (1re inspection) :
- exécution 2010–2012 « par titre » : une ligne par action, colonnes t1…t7 en €
  (2010 en nomenclature ministère, 2011 en mission, 2012 en mission sans total) ;
- annexes PLRG 2024 : `annexe1_etat_titre_cat` (programme × titre × catégorie, €
  à virgule), `annexe1_etat_cp` (programme × T2/hors T2, sert de contrôle),
  `annexe1_etat_recettes` (recettes par section, totaux nets en colonne
  « Total des recettes** ») ;
- PLF/LFI : recettes par ligne (2024, 2025), dépenses selon destination (2025) ;
- INSEE BDM (SDMX) : dette négociable de l'État.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

Row = tuple[str, dict]

# Libellés des titres de dépenses (art. 5 de la LOLF).
TITRES = {
    "1": "Dotations des pouvoirs publics",
    "2": "Dépenses de personnel",
    "3": "Dépenses de fonctionnement",
    "4": "Charges de la dette de l'État",
    "5": "Dépenses d'investissement",
    "6": "Dépenses d'intervention",
    "7": "Dépenses d'opérations financières",
}


class UnknownFormat(Exception):
    """Les en-têtes ne correspondent à aucun format connu."""


class Skip(Exception):
    """Fichier volontairement ignoré (doublon AE, compte spécial…)."""


# --- utilitaires --------------------------------------------------------------

def decode_bytes(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise AssertionError("latin-1 décode toujours")  # pragma: no cover


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    text, _ = decode_bytes(path.read_bytes())
    first = text.split("\n", 1)[0]
    delim = max((";", ",", "\t"), key=first.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in reader]
    return headers, rows


def to_float(v) -> float | None:
    """'1 234,56' / '1234.0' / '-8836054,82' -> float ; vide -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[\s  ]", "", str(v))
    if s in ("", "-", "so", "nd"):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def meur(v) -> float | None:
    x = to_float(v)
    return None if x is None else x / 1e6


def year_from_path(path: Path) -> int:
    for part in reversed(path.parts):
        m = re.search(r"(?:^|[-_])((?:19|20)\d{2})(?:[-_]|$)", part)
        if m:
            return int(m.group(1))
    raise UnknownFormat(f"exercice introuvable dans {path}")


def split_programme(s: str) -> tuple[str | None, str]:
    """'Action de la France en Europe et dans le monde - 105' -> ('105', 'Action …')."""
    m = re.match(r"^(.*?)\s*-\s*(\d{3})\s*$", s or "")
    return (m.group(2), m.group(1)) if m else (None, s)


def perimetre_programme(code: str | None) -> str:
    """Budget de rattachement selon la plage de numéros de programme.

    Convention de numérotation : < 600 budget général, 6xx budgets annexes,
    7xx comptes d'affectation spéciale, 8xx comptes de concours financiers,
    9xx comptes de commerce / opérations monétaires.
    """
    if not code or not code.isdigit():
        return "inconnu"
    n = int(code)
    return "BG" if n < 600 else {6: "BA", 7: "CAS", 8: "CCF"}.get(n // 100, "CC")


def _norm_header(h: str) -> str:
    import unicodedata

    h = unicodedata.normalize("NFKD", h).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")


def _dep(exercice, mission, code, lib, titre, montant, source, categorie=None, nature="execution") -> Row:
    return "depense", dict(
        exercice=exercice, mois=None, periode="annuel", nature=nature, mission_code=None,
        mission_lib=mission or None, programme_code=code, programme_lib=lib,
        titre_code=titre, titre_lib=TITRES.get(titre) if titre else None, categorie_code=categorie,
        montant_meur=montant, source=source)


# --- exécution « par titre » (2010–2012, et années de même format) -------------

EXEC_ALIASES = {
    "programme": ("programme", "pgm", "code_programme", "programme_code", "num_programme"),
    "programme_lib": ("libelle_programme", "libelle_pgm", "lib_programme"),
    "mission": ("mission", "libelle_mission", "lib_mission"),
}


def parse_exec_titres(path: Path) -> Iterator[Row]:
    name = path.parent.name + "/" + path.name if path.name == "export.csv" else path.name
    n = _norm_header(name)
    if not re.search(r"en_cp|exec_msn_cp", n) or re.search(r"ae_et_cp|ministere|compte", n):
        raise Skip("pas un fichier CP du budget général en nomenclature mission")
    headers, rows = read_rows(path)
    h = {_norm_header(x): x for x in headers}
    pick = lambda key: next((h[a] for a in EXEC_ALIASES[key] if a in h), None)
    prog, lib, mission = pick("programme"), pick("programme_lib"), pick("mission")
    titres = [(str(k), h[f"t{k}"]) for k in range(1, 8) if f"t{k}" in h]
    if not prog or len(titres) < 5:
        raise UnknownFormat(f"en-têtes {headers}")
    ex = year_from_path(path)
    agg: dict[tuple, float] = defaultdict(float)
    labels: dict[tuple, tuple] = {}
    for r in rows:
        code = str(r[prog]).strip().zfill(3)
        if perimetre_programme(code) != "BG":
            continue
        for t, col in titres:
            v = meur(r.get(col))
            if v is None:
                continue
            key = (code, t)
            agg[key] += v
            labels[key] = (r.get(mission) if mission else None, r.get(lib))
    src = f"data.economie {path.parent.name}"
    for (code, t), v in sorted(agg.items()):
        m, l = labels[(code, t)]
        yield _dep(ex, m, code, l, t, v, src)


# --- PLRG : annexes de l'état budgétaire --------------------------------------

def parse_plrg_titre_cat(path: Path) -> Iterator[Row]:
    headers, rows = read_rows(path)
    if not {"Mission", "Programme", "Titre", "Categorie", "Depenses"} <= set(headers):
        raise UnknownFormat(f"en-têtes {headers}")
    ex = year_from_path(path)
    for r in rows:
        code, lib = split_programme(r["Programme"])
        titre = re.sub(r"\D", "", r["Titre"]) or None
        v = meur(r["Depenses"])
        if v is None:
            continue
        yield _dep(ex, r["Mission"], code, lib, titre, v, f"PLRG {ex} annexe1_etat_titre_cat",
                   categorie=r["Categorie"] or None)


def parse_plrg_cp(path: Path) -> Iterator[Row]:
    """Dépenses constatées par programme : sert de contrôle de cohérence."""
    headers, rows = read_rows(path)
    if not {"Mission", "Programme", "Depenses_constatees"} <= set(headers):
        raise UnknownFormat(f"en-têtes {headers}")
    ex = year_from_path(path)
    tot: dict[str, float] = defaultdict(float)
    for r in rows:
        code, _ = split_programme(r["Programme"])
        tot[code] += meur(r["Depenses_constatees"]) or 0.0
    for code, v in tot.items():
        yield "controle", dict(exercice=ex, cle=f"programme {code}", reference=v,
                               source=f"PLRG {ex} annexe1_etat_cp (dépenses constatées)")


CATEGORIES_RECETTES = [
    (r"^recettes fiscales", "fiscale"),
    (r"^recettes non fiscales", "non_fiscale"),
    (r"^pr[ée]l[èe]vements? sur les recettes", "prelevement"),
    (r"^fonds de concours", "fonds_concours"),
]


def _categorie(s: str) -> str:
    for rx, c in CATEGORIES_RECETTES:
        if re.search(rx, s or "", re.I):
            return c
    return "autre"


def parse_plrg_recettes(path: Path) -> Iterator[Row]:
    """Totaux par section (« 11 - Impôt sur le revenu (total) ») de la colonne
    « Total des recettes** ». Les prélèvements, négatifs dans la source, sont
    stockés en positif (convention du schéma). Les totaux de catégorie servent
    de contrôle."""
    headers, rows = read_rows(path)
    col = next((x for x in headers if x.startswith("Total des recettes")), None)
    if not col or not {"Catégorie", "Section"} <= set(headers):
        raise UnknownFormat(f"en-têtes {headers}")
    ex = year_from_path(path)
    src = f"PLRG {ex} annexe1_etat_recettes"
    sections: dict[str, list[tuple[bool, float, str]]] = defaultdict(list)
    for r in rows:
        v = meur(r[col])
        if not v:
            continue
        cat, sec = r["Catégorie"], r["Section"]
        if cat.endswith("(total)"):
            c = _categorie(cat)
            yield "controle", dict(exercice=ex, cle=f"recettes {c}", source=src,
                                   reference=-v if c == "prelevement" else v)
            continue
        base = re.sub(r"\s*\(total\)$", "", sec)
        sections[base].append((sec.endswith("(total)"), v, cat))
    for sec, vals in sections.items():
        totals = [v for is_tot, v, _ in vals if is_tot]
        v = totals[0] if totals else sum(v for _, v, _ in vals)
        c = _categorie(vals[0][2])
        m = re.match(r"^(\d+)\s*-\s*(.*)$", sec)
        code, lib = (m.group(1), m.group(2)) if m else (None, sec)
        yield "recette", dict(exercice=ex, mois=None, periode="annuel", nature="execution", categorie=c,
                              sens=None, poste_code=code, poste_lib=lib,
                              montant_meur=-v if c == "prelevement" else v, source=src)


# --- PLF / LFI -----------------------------------------------------------------

def parse_plf_recettes(path: Path) -> Iterator[Row]:
    headers, rows = read_rows(path)
    if not {"annee", "type_de_recettes", "libelle"} <= set(headers):
        raise UnknownFormat(f"en-têtes {headers}")
    cols = [(c, c.rsplit("_", 1)[-1]) for c in headers if c.startswith("montant_recettes_")]
    for r in rows:
        ex = int(to_float(r["annee"]))
        c = _categorie(r["type_de_recettes"])
        code = r.get("code_ligne_recettes")
        code = str(int(to_float(code))) if code else None
        for col, nature in cols:
            v = meur(r[col])
            if v is None:
                continue
            yield "recette", dict(exercice=ex, mois=None, periode="annuel", nature=nature, categorie=c, sens=None,
                                  poste_code=code, poste_lib=r["libelle"], montant_meur=v,
                                  source=f"data.economie {path.parent.name}")


def parse_plf_depenses(path: Path) -> Iterator[Row]:
    headers, rows = read_rows(path)
    need = {"exercice", "loi", "typebudget", "libelle_mission", "programme", "libelle_programme", "titre",
            "categorie", "credit_de_paiement"}
    if not need <= set(headers):
        raise UnknownFormat(f"en-têtes {headers}")
    agg: dict[tuple, float] = defaultdict(float)
    for r in rows:
        if r["typebudget"] != "BG":
            continue
        key = (int(r["exercice"]), r["loi"].lower(), r["libelle_mission"], str(r["programme"]).zfill(3),
               r["libelle_programme"], str(r["titre"]), str(r["categorie"]))
        agg[key] += meur(r["credit_de_paiement"]) or 0.0
    for (ex, loi, m, code, lib, t, cat), v in agg.items():
        yield _dep(ex, m, code, lib, t, v, f"data.economie {path.parent.name}", categorie=cat, nature=loi)


# --- INSEE (SDMX) -------------------------------------------------------------

def parse_sdmx(data: bytes) -> list[dict]:
    """Séries SDMX-ML (structure-specific ou générique) -> [{attrs, obs}]."""
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "Series":
            continue
        obs = [dict(o.attrib) for o in el if o.tag.rsplit("}", 1)[-1] == "Obs"]
        out.append({"attrs": dict(el.attrib), "obs": sorted(obs, key=lambda o: o.get("TIME_PERIOD", ""))})
    return out


# Série retenue pour la dette de l'État : titre contenant « dette négociable »
# sans précision de maturité (court / moyen / long terme).
DETTE_TITRE = re.compile(r"dette n[ée]gociable", re.I)
DETTE_EXCLU = re.compile(r"court|moyen|long|terme|maturit|vie moyenne|taux|indexée", re.I)


def parse_insee(path: Path) -> Iterator[Row]:
    for s in parse_sdmx(path.read_bytes()):
        a = s["attrs"]
        idbank, titre = a.get("IDBANK"), a.get("TITLE_FR", "")
        for o in s["obs"]:
            v = to_float(o.get("OBS_VALUE"))
            if v is None:
                continue
            yield "serie", dict(source="INSEE", serie=idbank, titre=titre, periode=o["TIME_PERIOD"], valeur=v,
                                unite=a.get("UNIT_MEASURE"), puissance=to_float(a.get("UNIT_MULT")))
        if DETTE_TITRE.search(titre) and not DETTE_EXCLU.search(titre):
            if a.get("UNIT_MULT") is None:
                raise UnknownFormat(f"série {idbank} sans UNIT_MULT : unité inconnue ({a})")
            mult = 10 ** int(a["UNIT_MULT"])
            for o in s["obs"]:
                m = re.match(r"^(\d{4})-(\d{2})$", o.get("TIME_PERIOD", ""))
                v = to_float(o.get("OBS_VALUE"))
                if m and v is not None:
                    yield "agregat_etat", dict(exercice=int(m.group(1)), mois=int(m.group(2)), periode="cumul_mensuel",
                                               indicateur="dette_etat", montant_meur=v * mult / 1e6,
                                               source=f"INSEE {idbank} (dette négociable, AFT)")


def parse_plr_attachment(path: Path) -> Iterator[Row]:
    """Aiguillage des pièces jointes PLR / PLRG d'après le nom de fichier."""
    n = path.name.lower()
    if "annexe1_etat_titre_cat" in n:
        yield from parse_plrg_titre_cat(path)
    elif re.search(r"annexe1_etat_cp_\d{4}", n):
        yield from parse_plrg_cp(path)
    elif "annexe1_etat_recettes" in n:
        yield from parse_plrg_recettes(path)
    elif "exec_msn_cp" in n:
        yield from parse_exec_titres(path)
    else:
        raise Skip("pièce jointe non utilisée")
