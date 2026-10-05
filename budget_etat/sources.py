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
    # UTF-16 (fichiers SMB 2013-2023) : BOM FF FE / FE FF, ou à défaut beaucoup d'octets nuls.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    if data[:200].count(b"\x00") > 40:
        return data.decode("utf-16-le" if data[1:2] == b"\x00" else "utf-16-be"), "utf-16"
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise AssertionError("latin-1 décode toujours")  # pragma: no cover


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    text, _ = decode_bytes(path.read_bytes())
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    first = text.split("\n", 1)[0]
    delim = max((";", ",", "\t"), key=first.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in reader]
    return headers, rows


def read_sheets(path: Path) -> list[tuple[str, list[list[str]]]]:
    """Feuilles d'un fichier tabulaire (CSV, .xls BIFF ou .xlsx) en grilles de chaînes."""
    data = path.read_bytes()
    if data[:4] == b"\xd0\xcf\x11\xe0":  # OLE2 : Excel 97-2003
        import xlrd

        book = xlrd.open_workbook(file_contents=data)
        return [(sh.name, [["" if c.value is None else str(c.value).strip() for c in sh.row(i)]
                           for i in range(sh.nrows)]) for sh in book.sheets()]
    if data[:2] == b"PK":  # OOXML : .xlsx
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return [(ws.title, [["" if v is None else str(v).strip() for v in row] for row in ws.iter_rows(values_only=True)])
                for ws in wb.worksheets]
    text, _ = decode_bytes(data)
    # Certains fichiers DGFiP ont des fins de ligne CR seules (ancien format Mac).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    first = text.split("\n", 1)[0]
    delim = max((";", ",", "\t"), key=first.count)
    return [("csv", [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delim)])]


def find_header(grid: list[list[str]], required: set[str], max_rows: int = 30) -> int | None:
    """Indice de la première ligne dont les en-têtes normalisés contiennent `required`."""
    for i, row in enumerate(grid[:max_rows]):
        if required <= {_norm_header(c) for c in row}:
            return i
    return None


def grid_rows(grid: list[list[str]], header: int) -> tuple[list[str], list[dict]]:
    headers = [_norm_header(c) for c in grid[header]]
    return headers, [dict(zip(headers, r)) for r in grid[header + 1:] if any(c for c in r)]


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


def try_float(v) -> float | None:
    """Comme to_float, mais None pour un texte non numérique (cellules de libellé)."""
    try:
        return to_float(v)
    except ValueError:
        return None


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

    # Apostrophe typographique (’) : séparateur comme « ' », sinon « d’information » -> « dinformation ».
    h = h.replace("\u2019", "'").replace("\u02bc", "'")
    h = unicodedata.normalize("NFKD", h).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")


ODS_VIDE = {"recordid", "record_timestamp", "resource_id"}


def skip_if_empty_ods_export(headers: list[str], n_rows: int) -> None:
    """Export ODS d'un jeu sans enregistrements (données en pièces jointes seulement)."""
    if ODS_VIDE <= {_norm_header(h) for h in headers} and n_rows == 0:
        raise Skip("export vide : le jeu ne publie que des pièces jointes")


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
    (_, grid), = read_sheets(path)[:1]
    ex = year_from_path(path)
    # Export « tableau croisé » (2013, 2014) : lignes de titre puis l'en-tête réel.
    hdr = find_header(grid, {"mission", "programme", "t1", "t2"})
    if hdr is not None and hdr > 0:
        years = [int(c) for row in grid[:hdr] for c in row if re.fullmatch(r"(19|20)\d{2}", c)]
        ex = years[0] if years else ex
    else:
        hdr = 0
    headers, rows = grid_rows(grid, hdr)
    skip_if_empty_ods_export(grid[hdr], len(rows))
    h = set(headers)
    pick = lambda key: next((a for a in EXEC_ALIASES[key] if a in h), None)
    prog, lib, mission = pick("programme"), pick("programme_lib"), pick("mission")
    titres = [(str(k), f"t{k}") for k in range(1, 8) if f"t{k}" in h]
    if not prog or len(titres) < 5:
        raise UnknownFormat(f"en-têtes {grid[hdr]}")
    agg: dict[tuple, float] = defaultdict(float)
    labels: dict[tuple, tuple] = {}
    for r in rows:
        code = prog_code(r.get(prog))
        if code is None:
            continue  # lignes de total, de titre ou vides
        if perimetre_programme(code) != "BG":
            continue
        for t, col in titres:
            v = meur(r.get(col))
            if v is None:
                continue
            key = (code, t)
            agg[key] += v
            labels[key] = (r.get(mission) if mission else None, r.get(lib))
    src = f"data.economie {path.parent.name if path.name == 'export.csv' else path.name}"
    for (code, t), v in sorted(agg.items()):
        m, l = labels[(code, t)]
        yield _dep(ex, m, code, l, t, v, src)


# --- exécution « destination × nature » (PLR 2018 et suivants) ------------------

def _exec_cp_column(headers: list[str]) -> tuple[str, int] | None:
    for h in headers:
        m = re.fullmatch(r"exec_cp_((?:19|20)\d{2})(?:_.*)?", h)
        if m:
            return h, int(m.group(1))
    return None


def _is_bg(r: dict, code: str) -> bool:
    tb = next((v for k, v in r.items() if k.startswith("type_de_budget") or k == "typebudget"), None)
    if tb:
        return _norm_header(tb) in ("bg", "budget_general")
    return perimetre_programme(code) == "BG"


def _code(v) -> str:
    """'105.0' -> '105' ; '22.0' -> '22'."""
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def prog_code(v) -> str | None:
    """Numéro de programme normalisé sur 3 chiffres, ou None (vide, texte, 0 :
    lignes de total ou d'en-tête). « 105.0 » -> « 105 »."""
    c = _code(v)
    return c.zfill(3) if c.isdigit() and 0 < int(c) < 1000 else None


def load_nomenclature(directory: Path) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Classeur « nomenclature » joint aux PLR 2019+ : missions (MSN) et programmes (PGM)."""
    missions: dict[str, str] = {}
    programmes: dict[str, tuple[str, str]] = {}
    for f in sorted(directory.glob("*nomenclature*")):
        for _, grid in read_sheets(f):
            hdr = find_header(grid, {"type_ligne", "code", "mission", "libelle"})
            if hdr is None:
                continue
            for r in grid_rows(grid, hdr)[1]:
                if r.get("type_ligne") == "MSN":
                    missions[r["code"]] = r["libelle"]
                elif r.get("type_ligne") == "PGM":
                    if prog_code(r["code"]):
                        programmes[prog_code(r["code"])] = (r["libelle"], r.get("mission", ""))
    return missions, programmes


# Deux variantes d'en-têtes observées pour « destination × nature » :
# PLR 2018 (export CSV) : code_programme, code_titre, code_categorie, exec_cp_2018_rap_2018, annee_rap, libellés ;
# PLR 2019–2020 (xls)   : programme, titre, categorie, « CP EXEC », exercice, mission = code (libellés à part).
DEST_NAT_VARIANTES = [
    {"programme": "code_programme", "titre": "code_titre", "categorie": "code_categorie", "exercice": "annee_rap"},
    {"programme": "programme", "titre": "titre", "categorie": "categorie", "exercice": "exercice", "cp": "cp_exec"},
]


def parse_destination_nature(path: Path) -> Iterator[Row]:
    """Une ligne par programme × action × (sous-action) × catégorie, CP exécutés en €."""
    for _, grid in read_sheets(path):
        for v in DEST_NAT_VARIANTES:
            need = {v["programme"], v["titre"], v["categorie"]} | ({v["cp"]} if "cp" in v else set())
            hdr = find_header(grid, need)
            if hdr is not None:
                break
        else:
            continue
        headers, rows = grid_rows(grid, hdr)
        if "cp" in v:
            col, ex = v["cp"], None
        else:
            found = _exec_cp_column(headers)
            if not found:
                raise UnknownFormat(f"pas de colonne exec_cp_AAAA : {grid[hdr]}")
            col, ex = found
        missions, programmes = load_nomenclature(path.parent) if "cp" in v else ({}, {})
        years = {int(_code(r.get(v["exercice"]))) for r in rows if _code(r.get(v["exercice"])).isdigit()}
        if ex is None and len(years) == 1:
            ex = years.pop()  # quelques lignes sans exercice dans un fichier mono-exercice
        # Colonne « loi » : ne garder que l'exécution (PLR / PLRG) si d'autres lois
        # (LFI, LFR…) sont mélangées dans le même classeur.
        lois = {str(r.get("loi") or "").strip().upper() for r in rows if r.get("loi")}
        autres_lois = sorted(lois - {"PLR", "PLRG"})
        if "loi" in headers and lois and not lois & {"PLR", "PLRG"}:
            raise UnknownFormat(f"aucune ligne d'exécution (PLR) : lois présentes {sorted(lois)}")
        if autres_lois:
            rows = [r for r in rows if str(r.get("loi") or "").strip().upper() in ("PLR", "PLRG", "")]
            yield "avertissement", {"message": f"lignes des lois {autres_lois} écartées (seule l'exécution PLR est gardée)"}
        agg: dict[tuple, float] = defaultdict(float)
        bad: list[dict] = []
        for r in rows:
            code = prog_code(r[v["programme"]])
            if code is None or not _is_bg(r, code):  # ligne de total (programme vide) ou parasite
                continue
            e = _code(r.get(v["exercice"]))
            e = int(e) if e.isdigit() else ex
            if e is None:
                raise UnknownFormat(f"exercice introuvable (exercices présents : {sorted(years)})")
            m = r.get("mission")
            lib = r.get("programme") if v["programme"] == "code_programme" else None
            if code in programmes:
                lib, mcode = programmes[code]
                m = missions.get(mcode, m)
            elif m in missions:
                m = missions[m]
            key = (e, m, code, lib, _code(r[v["titre"]]), _code(r[v["categorie"]]))
            val = try_float(r[col])
            if val is None and str(r[col] or "").strip():
                bad.append(r)  # texte parasite dans la colonne de montant (ex. « Expr2 »)
                continue
            agg[key] += (val or 0.0) / 1e6
        if len(bad) > max(1, len(rows) // 100):
            raise UnknownFormat(f"{len(bad)} montants non numériques sur {len(rows)} lignes, ex. {bad[0]}"[:400])
        if bad:
            yield "avertissement", {"message": f"{len(bad)} ligne(s) au montant non numérique ignorée(s), "
                                               f"ex. {bad[0]}"[:300]}
        src = f"PLR {{e}} destination × nature ({path.parent.name})"
        for (e, m, code, lib, t, cat), val in agg.items():
            yield _dep(e, m, code, lib, t, val, src.format(e=e), categorie=cat)
        return
    raise UnknownFormat("aucune feuille au format destination × nature")


def parse_plr_export(path: Path) -> Iterator[Row]:
    """Exports CSV des jeux « projet de loi de règlement » : trois variantes."""
    (_, grid), = read_sheets(path)[:1]
    headers = [_norm_header(c) for c in grid[0]] if grid else []
    skip_if_empty_ods_export(grid[0] if grid else [], sum(1 for r in grid[1:] if any(r)))
    if {"code_programme", "code_titre", "code_categorie"} <= set(headers):
        yield from parse_destination_nature(path)
    elif any(re.fullmatch(r"exec_cp_t2_hors_t2_\d{4}.*", h) for h in headers):
        # Synthèse par programme (T2 + hors T2) : sert de contrôle.
        col = next(h for h in headers if re.fullmatch(r"exec_cp_t2_hors_t2_\d{4}.*", h))
        _, rows = grid_rows(grid, 0)
        for r in rows:
            code = prog_code(r["code_programme"])
            if code is None or not _is_bg(r, code):
                continue
            ex = int(to_float(r["annee_rap"]))
            yield "controle", dict(exercice=ex, cle=f"programme {code}", reference=meur(r[col]) or 0.0,
                                   source=f"PLR {ex} synthèse par programme ({path.parent.name})")
    elif any(re.fullmatch(r"t\d_exec_cp_\d{4}", h) for h in headers):
        raise Skip("tableau croisé par titre : doublon de destination × nature")
    else:
        raise UnknownFormat(f"en-têtes {grid[0] if grid else []}")


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
    norm = {_norm_header(h) for h in headers}
    if not col and norm & {"total_recouvrement", "total_des_recouvrements"}:
        # PLRG 2023 / 2025 : recouvrements bruts par ligne d'exécution, sans totaux nets.
        raise Skip("recouvrements bruts sans totaux nets : recettes de l'exercice prises dans la SMB")
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


# --- PLF : tableau « recettes fiscales nettes » (colonne Exécution N-2) ----------

RECETTES_NETTES = [
    # (motif sur le libellé normalisé, catégorie, code, libellé canonique)
    (r"^1\.\s*impot sur le revenu net", "fiscale", "IR", "Impôt sur le revenu (net)"),
    (r"^2\.\s*impot sur les societes net", "fiscale", "IS", "Impôt sur les sociétés (net)"),
    (r"^3\.\s*ticpe", "fiscale", "TICPE", "TICPE"),
    (r"^4\.\s*taxe sur la valeur ajoutee.*nette", "fiscale", "TVA", "Taxe sur la valeur ajoutée (nette)"),
    (r"^5\.\s*autres recettes fiscales.*nettes", "fiscale", "AUTRES", "Autres recettes fiscales (nettes)"),
    (r"^d\.\s*recettes non fiscales", "non_fiscale", "NF", "Recettes non fiscales"),
    (r"au profit des collectivites", "prelevement", "PSR_COLL", "Prélèvements au profit des collectivités territoriales"),
    (r"au profit de l.?union europeenne", "prelevement", "PSR_UE", "Prélèvement au profit de l'Union européenne"),
]
RECETTES_NETTES_CONTROLES = [(r"^c\.\s*recettes fiscales nettes", "recettes fiscale"),
                             (r"^e\.\s*prelevements", "recettes prelevement")]


def _plain(s: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def parse_recettes_nettes(path: Path) -> Iterator[Row]:
    (_, grid), = read_sheets(path)[:1]
    unit = _plain(grid[0][0]) if grid and grid[0] else ""
    factor = 1e-3 if "milliers" in unit else 1.0 if "million" in unit else None
    if factor is None:
        raise UnknownFormat(f"unité introuvable dans {grid[0][:1] if grid else None}")
    col = ex = None
    for i, row in enumerate(grid[:6]):
        for j, c in enumerate(row):
            if _plain(c).startswith("execution"):
                col = j
                m = re.search(r"(19|20)\d{2}", c) or re.search(r"(19|20)\d{2}", grid[i + 1][j] if i + 1 < len(grid) else "")
                ex = int(m.group(0)) if m else None
                break
        if col is not None:
            break
    if col is None or ex is None:
        raise UnknownFormat("colonne « Exécution AAAA » introuvable")
    src = f"PLF {ex + 2} recettes fiscales nettes (exécution {ex})"
    found = {}
    for row in grid:
        label = _plain(" ".join(c for c in row[:col] if c))
        v = try_float(row[col]) if col < len(row) else None
        if not label or v is None:
            continue
        for rx, cat, code, lib in RECETTES_NETTES:
            if re.search(rx, label) and code not in found:
                found[code] = (cat, lib, v * factor)
        for rx, cle in RECETTES_NETTES_CONTROLES:
            if re.search(rx, label):
                yield "controle", dict(exercice=ex, cle=cle, reference=v * factor, source=src)
    if not {"IR", "IS", "TVA", "NF"} <= set(found):
        labels = " ".join(_plain(" ".join(r[:col])) for r in grid)
        if "impot net sur le revenu" in labels:
            raise UnknownFormat("tableau PLF 2012 incomplet : ni recettes non fiscales, ni prélèvements, ni "
                                "remboursements d'impôts locaux ; exécution 2010 non ingérée (total net impossible)")
        raise UnknownFormat(f"lignes nettes non reconnues (trouvées : {sorted(found)})")
    for code, (cat, lib, v) in found.items():
        yield "recette", dict(exercice=ex, mois=None, periode="annuel", nature="execution", categorie=cat, sens="net",
                              poste_code=code, poste_lib=lib, montant_meur=v, source=src)


# --- SMB : situations mensuelles budgétaires (DGFiP), séries longues ----------

# (motif sur la ligne d'information normalisée, indicateur agrégé, ligne de recette)
SMB_LIGNES = [
    (r"^solde budgetaire$", "solde", None),
    (r"^total depenses nettes du budget general$", "depenses_nettes", None),
    (r"^dotation des pouvoirs publics$", "depenses_titre_1", None),
    (r"^depenses de personnel$", "depenses_titre_2", None),
    (r"^depenses de fonctionnement$", "depenses_titre_3", None),
    (r"^charges? de la dette de l.?etat$", "charge_dette", None),
    (r"^depenses d.?investissement$", "depenses_titre_5", None),
    (r"^depenses d.?intervention$", "depenses_titre_6", None),
    (r"^depenses d.?operations financieres$", "depenses_titre_7", None),
    (r"^total prelevements sur recettes$", "prelevements", None),
    (r"^psr au profit des collectivites", "psr_collectivites", ("prelevement", "PSR_COLL", "Prélèvements au profit des collectivités territoriales")),
    (r"^psr au profit de l.?union europeenne", "psr_ue", ("prelevement", "PSR_UE", "Prélèvement au profit de l'Union européenne")),
    (r"^total recettes nettes du budget general$", "recettes_nettes_bg", None),
    (r"^total recettes fiscales$", "recettes_fiscales_nettes", None),
    (r"^impot sur le revenu$", None, ("fiscale", "IR", "Impôt sur le revenu (net)")),
    (r"^impot sur les societes$", None, ("fiscale", "IS", "Impôt sur les sociétés (net)")),
    (r"^taxe interieure de consommation sur les produits energetiques$", None, ("fiscale", "TICPE", "TICPE")),
    (r"^taxe sur la valeur ajoutee$", None, ("fiscale", "TVA", "Taxe sur la valeur ajoutée (nette)")),
    (r"^autres recettes fiscales$", None, ("fiscale", "AUTRES", "Autres recettes fiscales (nettes)")),
    (r"^total recettes non fiscales$", "recettes_non_fiscales", ("non_fiscale", "NF", "Recettes non fiscales")),
    (r"^fonds de concours", "fonds_concours", ("fonds_concours", "FDC", "Fonds de concours et attributions de produits")),
    (r"^solde des comptes speciaux$", "solde_comptes_speciaux", None),
    (r"^solde des budgets annexes$", "solde_budgets_annexes", None),
    (r"^remboursements et degrevements d.?impots d.?etat$", "rd_impots_etat", None),
    (r"^remboursements et degrevements d.?impots locaux$", "rd_impots_locaux", None),
]


def _smb_date(h: str) -> tuple[int, int] | None:
    """En-tête de colonne -> (année, mois). Formats : jj_mm_aaaa (observé), jj/mm/aaaa,
    aaaa-mm-jj, aaaa-mm, mm/aaaa."""
    h = h.strip().split(" ")[0]
    for rx, y, mo in [(r"(\d{1,2})[_/.-](\d{1,2})[_/.-]((?:19|20)\d{2})", 3, 2),
                      (r"((?:19|20)\d{2})[_/.-](\d{1,2})(?:[_/.-]\d{1,2})?", 1, 2),
                      (r"(\d{1,2})[_/.-]((?:19|20)\d{2})", 2, 1)]:
        m = re.fullmatch(rx, h)
        if m and 1 <= int(m.group(mo)) <= 12:
            return int(m.group(y)), int(m.group(mo))
    return None


def parse_smb(path: Path) -> Iterator[Row]:
    """Tableau large : une ligne par « ligne d'information », une colonne par fin de mois
    (cumul depuis le 1er janvier, en €). Produit : agrégats mensuels (solde budgétaire
    officiel, dépenses et recettes nettes…), recettes par grande catégorie, et la
    série brute de chaque ligne."""
    for _, grid in read_sheets(path):
        hdr = find_header(grid, {"ligne_d_information"})
        if hdr is None:
            continue
        raw_headers = grid[hdr]
        cols = [(i, _smb_date(h)) for i, h in enumerate(raw_headers)]
        cols = [(i, d) for i, d in cols if d]
        if not cols:
            raise UnknownFormat(f"aucune colonne de date jj_mm_aaaa : {raw_headers[:8]}")
        li = [_norm_header(h) for h in raw_headers].index("ligne_d_information")
        src = f"SMB DGFiP ({path.name})"
        for row in grid[hdr + 1:]:
            if li >= len(row) or not row[li]:
                continue
            label = row[li]
            norm = _plain(label).replace("’", "'")
            match = next(((ind, rec) for rx, ind, rec in SMB_LIGNES if re.search(rx, norm)), (None, None))
            for i, (ex, mois) in cols:
                v = meur(row[i]) if i < len(row) else None
                if v is None:
                    continue
                yield "serie", dict(source="SMB", serie=label, titre=label, periode=f"{ex}-{mois:02d}",
                                    valeur=v, unite="M€", puissance=0)
                ind, rec = match
                if ind:
                    yield "agregat_etat", dict(exercice=ex, mois=mois, periode="cumul_mensuel", indicateur=ind,
                                               montant_meur=v, source=src)
                if rec:
                    cat, code, lib = rec
                    yield "recette", dict(exercice=ex, mois=mois, periode="cumul_mensuel", nature="execution",
                                          categorie=cat, sens="net", poste_code=code, poste_lib=lib,
                                          montant_meur=v, source=src)
        return
    first = read_sheets(path)[0][1][:2] if read_sheets(path) else []
    raise UnknownFormat(f"pas de colonne « ligne d'information » ; premières lignes : {first}"[:400])


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
        if r["typebudget"] != "BG" or prog_code(r["programme"]) is None:
            continue
        key = (int(r["exercice"]), r["loi"].lower(), r["libelle_mission"], prog_code(r["programme"]),
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


# Série retenue pour la dette de l'État (identifiée à la 2e inspection) :
# « Encours de la dette négociable totale de l'État », mensuelle, UNIT_MULT=6.
# Les autres séries de la famille (maturités, devises, variations) sont
# conservées dans la table `serie` mais jamais additionnées.
DETTE_IDBANK = "001739081"


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
        if idbank == DETTE_IDBANK:
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
    elif "credits_destination_nature" in n:
        yield from parse_destination_nature(path)
    else:
        raise Skip("pièce jointe non utilisée")
