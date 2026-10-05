"""Requêtes de lecture sur la base canonique, pour le front et le simulateur.

Conventions :
- « exécution annuelle » = lignes periode='annuel' ; à défaut, le cumul à fin
  décembre de la SME (periode='cumul_mensuel' et mois=12).
- Les montants des prélèvements sur recettes sont stockés positifs ; ils sont
  retranchés des recettes du budget général.
"""

from __future__ import annotations

import duckdb

from budget_etat import rigidites
from budget_etat.rigidites import CHARGE_DETTE_PROGRAMMES

PERIMETRE_ETAT = "État – budget général (DGFiP)"
PERIMETRE_APU = "Administrations publiques – Maastricht (Eurostat)"


def _annual(table: str, extra: str = "") -> str:
    """Sous-requête sélectionnant l'exécution annuelle d'une table."""
    return f"""(
        SELECT * FROM {table} t WHERE nature = 'execution' {extra} AND (
            periode = 'annuel' OR (periode = 'cumul_mensuel' AND mois = 12 AND NOT EXISTS (
                SELECT 1 FROM {table} a WHERE a.periode = 'annuel' AND a.nature = 'execution'
                AND a.exercice = t.exercice)))
    )"""


REC_NET = _annual("recette", "AND coalesce(sens, 'net') = 'net'")


def _annual_agregat() -> str:
    return """(
        SELECT * FROM agregat_etat t WHERE periode = 'annuel' OR (periode = 'cumul_mensuel' AND mois = 12
            AND NOT EXISTS (SELECT 1 FROM agregat_etat a WHERE a.periode = 'annuel'
                AND a.exercice = t.exercice AND a.indicateur = t.indicateur))
    )"""


def status(con: duckdb.DuckDBPyConnection) -> dict:
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("depense", "recette", "agregat_etat", "macro", "nomenclature_programme")}
    years = [r[0] for r in con.execute(f"SELECT DISTINCT exercice FROM {_annual('depense')} ORDER BY 1").fetchall()]
    sources = [dict(zip(("path", "url", "fetched_at", "parser"), r)) for r in
               con.execute("SELECT path, url, fetched_at, parser FROM source_file ORDER BY path").fetchall()]
    nomen = dict(con.execute("SELECT methode, count(*) FROM nomenclature_programme GROUP BY 1").fetchall())
    return {"counts": counts, "years": years, "sources": sources, "nomenclature": nomen}


def gdp(con: duckdb.DuckDBPyConnection) -> dict[int, float]:
    return dict(con.execute("SELECT annee, valeur_meur FROM macro WHERE indicateur = 'pib_nominal'").fetchall())


def series(con: duckdb.DuckDBPyConnection) -> dict:
    """Séries annuelles. Les deux périmètres sont renvoyés séparément."""
    dep = dict(con.execute(f"SELECT exercice, sum(montant_meur) FROM {_annual('depense')} GROUP BY 1").fetchall())
    charge = dict(con.execute(
        f"SELECT exercice, sum(montant_meur) FROM {_annual('depense')} WHERE programme_code IN "
        f"({', '.join(repr(c) for c in CHARGE_DETTE_PROGRAMMES)}) GROUP BY 1").fetchall())
    rec = dict(con.execute(
        f"SELECT exercice, sum(CASE WHEN categorie = 'prelevement' THEN -montant_meur ELSE montant_meur END) "
        f"FROM {REC_NET} GROUP BY 1").fetchall())
    agg: dict[str, dict[int, float]] = {}
    for ex, ind, v in con.execute(f"SELECT exercice, indicateur, montant_meur FROM {_annual_agregat()}").fetchall():
        agg.setdefault(ind, {})[ex] = v
    # La charge de la dette publiée en agrégat prime sur le programme 117 (qui inclut la trésorerie).
    charge = {**charge, **agg.get("charge_dette", {})}
    macro: dict[str, dict[int, float]] = {}
    for a, ind, v in con.execute("SELECT annee, indicateur, valeur_meur FROM macro").fetchall():
        macro.setdefault(ind, {})[a] = v
    years = sorted(set(dep) | set(rec) | set().union(*[set(d) for d in agg.values()] or [set()]))

    def col(d):
        return [d.get(y) for y in years]

    solde = agg.get("solde") or {y: rec[y] - dep[y] for y in years if y in rec and y in dep}
    pib = macro.get("pib_nominal", {})
    return {
        "etat": {
            "perimetre": PERIMETRE_ETAT,
            "years": years,
            "recettes_nettes": col(rec),
            "depenses": col(dep),
            "solde": col(solde),
            "solde_source": "agrégat publié" if agg.get("solde") else "recettes - dépenses (budget général)",
            "dette_etat": col(agg.get("dette_etat", {})),
            "charge_dette": col(charge),
            "pib": col(pib),
        },
        "apu": {
            "perimetre": PERIMETRE_APU,
            "years": sorted(set().union(*[set(d) for d in macro.values()] or [set()])),
            **{k: v for k, v in macro.items()},
        },
    }


def _gdp_for(con, exercice: int) -> float | None:
    return gdp(con).get(exercice)


def sankey(con: duckdb.DuckDBPyConnection, exercice: int, top_recettes: int = 12) -> dict:
    rec = con.execute(
        f"SELECT coalesce(poste_lib, poste_code, categorie), categorie, sum(montant_meur) FROM "
        f"{REC_NET} WHERE exercice = ? GROUP BY 1, 2 "
        f"ORDER BY 3 DESC", [exercice]).fetchall()
    dep = con.execute(
        f"SELECT coalesce(mission_lib, mission_code, '?'), coalesce(programme_lib, programme_code, '?'), "
        f"any_value(programme_code), sum(montant_meur) FROM {_annual('depense')} WHERE exercice = ? GROUP BY 1, 2",
        [exercice]).fetchall()
    BG = "Budget général"
    nodes: dict[str, dict] = {BG: {"name": BG, "kind": "bg"}}
    links = []
    ressources = [(n, c, v) for n, c, v in rec if c != "prelevement" and v > 0]
    head, tail = ressources[:top_recettes], ressources[top_recettes:]
    for name, cat, v in head:
        nodes.setdefault("R:" + name, {"name": "R:" + name, "label": name, "kind": "recette", "categorie": cat})
        links.append({"source": "R:" + name, "target": BG, "value": v})
    if tail:
        nodes["R:autres"] = {"name": "R:autres", "label": f"Autres recettes ({len(tail)})", "kind": "recette"}
        links.append({"source": "R:autres", "target": BG, "value": sum(v for *_, v in tail)})
    total_dep = sum(r[3] for r in dep)
    total_rec = sum(v for *_, v in ressources) - sum(v for _, c, v in rec if c == "prelevement")
    prelev = [(n, v) for n, c, v in rec if c == "prelevement" and v > 0]
    for name, v in prelev:  # les prélèvements sortent du budget général avant les missions
        key = "P:" + name
        nodes[key] = {"name": key, "label": name, "kind": "prelevement",
                      "rigidite": rigidites.flag("recette", poste_lib=name)}
        links.append({"source": BG, "target": key, "value": v})
    if total_dep > total_rec:  # le déficit finance le reste
        nodes["R:deficit"] = {"name": "R:deficit", "label": "Déficit (emprunt)", "kind": "deficit"}
        links.append({"source": "R:deficit", "target": BG, "value": total_dep - total_rec})
    missions: dict[str, float] = {}
    for m, p, code, v in dep:
        missions[m] = missions.get(m, 0) + v
        key = f"Pg:{m}/{p}"
        nodes[key] = {"name": key, "label": p, "kind": "programme",
                      "rigidite": rigidites.flag("depense", programme_code=code, programme_lib=p)}
        links.append({"source": "M:" + m, "target": key, "value": v})
    for m, v in missions.items():
        nodes["M:" + m] = {"name": "M:" + m, "label": m, "kind": "mission",
                           "rigidite": rigidites.flag("depense", mission_lib=m)}
        links.append({"source": BG, "target": "M:" + m, "value": v})
    return {"exercice": exercice, "perimetre": PERIMETRE_ETAT, "nodes": list(nodes.values()),
            "links": [l for l in links if l["value"] > 0]}


def drill(con: duckdb.DuckDBPyConnection, exercice: int, mission: str | None = None,
          programme: str | None = None) -> dict:
    """Mission -> programme -> titre, avec % du total et % du PIB."""
    base = f"{_annual('depense')}"
    total = con.execute(f"SELECT sum(montant_meur) FROM {base} WHERE exercice = ?", [exercice]).fetchone()[0] or 0
    pib = _gdp_for(con, exercice)
    if programme is not None:
        level, key = "titre", "coalesce(titre_lib, titre_code, 'Non ventilé')"
        where, params = "AND programme_code = ?", [programme]
        extra_code = "any_value(titre_code)"
    elif mission is not None:
        level, key = "programme", "coalesce(programme_lib, programme_code)"
        where, params = "AND coalesce(mission_lib, mission_code) = ?", [mission]
        extra_code = "any_value(programme_code)"
    else:
        level, key, where, params = "mission", "coalesce(mission_lib, mission_code)", "", []
        extra_code = "NULL"
    rows = con.execute(
        f"SELECT {key} AS k, {extra_code} AS code, sum(montant_meur) FROM {base} "
        f"WHERE exercice = ? {where} GROUP BY 1 ORDER BY 3 DESC", [exercice, *params]).fetchall()
    items = []
    for k, code, v in rows:
        fields = {"titre_code": code, "titre_lib": k} if level == "titre" else (
            {"programme_code": code, "programme_lib": k} if level == "programme" else {"mission_lib": k})
        items.append({"label": k, "code": code, "montant_meur": v,
                      "pct_total": v / total * 100 if total else None,
                      "pct_pib": v / pib * 100 if pib else None,
                      "rigidite": rigidites.flag("depense", **fields)})
    return {"exercice": exercice, "level": level, "mission": mission, "programme": programme,
            "total_meur": total, "pib_meur": pib, "perimetre": PERIMETRE_ETAT, "items": items}


def sim_base(con: duckdb.DuckDBPyConnection, exercice: int | None = None) -> dict:
    """Données de l'année de base du simulateur (lignes = programmes, groupes = missions).

    Les champs introuvables en base sont renvoyés à None : le front les rend éditables.
    """
    years = status(con)["years"]
    if exercice is None:
        exercice = years[-1] if years else None
    if exercice is None:
        return {"exercice": None, "gdp": None, "debt": None, "interest": None, "spending": [], "revenue": [],
                "missing": ["exercice", "gdp", "debt", "interest", "spending", "revenue"]}
    s = series(con)["etat"]
    i = s["years"].index(exercice) if exercice in s["years"] else None
    pick = (lambda k: s[k][i] if i is not None else None)
    spending = []
    for m, code, lib, titre, v in con.execute(
            f"SELECT coalesce(n.mission_canon, d.mission_lib, d.mission_code), d.programme_code, "
            f"any_value(coalesce(d.programme_lib, d.programme_code)), bool_or(d.titre_code = '2'), sum(d.montant_meur) "
            f"FROM {_annual('depense')} d LEFT JOIN nomenclature_programme n "
            f"ON n.exercice = d.exercice AND n.programme_code = d.programme_code "
            f"WHERE d.exercice = ? GROUP BY 1, 2 ORDER BY 1, 2", [exercice]).fetchall():
        if code in CHARGE_DETTE_PROGRAMMES:
            continue  # la charge d'intérêts est calculée par le modèle
        spending.append({"key": f"P:{code}", "label": f"{code} – {lib}", "group": f"M:{m}", "group_label": m,
                         "amount": v, "rigidite": rigidites.flag("depense", programme_code=code, programme_lib=lib)})
    revenue = []
    for cat, poste, v in con.execute(
            f"SELECT categorie, coalesce(poste_lib, poste_code), sum(montant_meur) FROM "
            f"{REC_NET} WHERE exercice = ? GROUP BY 1, 2 "
            f"ORDER BY 1, 3 DESC", [exercice]).fetchall():
        sign = -1 if cat == "prelevement" else 1
        revenue.append({"key": f"R:{poste}", "label": poste, "group": f"C:{cat}", "group_label": cat,
                        "amount": sign * v, "rigidite": rigidites.flag("recette", poste_lib=poste)})
    out = {"exercice": exercice, "gdp": _gdp_for(con, exercice), "debt": pick("dette_etat"),
           "interest": pick("charge_dette"), "spending": spending, "revenue": revenue,
           "perimetre": PERIMETRE_ETAT}
    out["missing"] = [k for k in ("gdp", "debt", "interest") if out[k] is None] + \
                     [k for k in ("spending", "revenue") if not out[k]]
    return out
