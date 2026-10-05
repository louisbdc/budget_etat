"""Normalisation des fichiers bruts vers la base canonique (data/budget.duckdb).

Idempotent : la base est reconstruite intégralement dans un fichier temporaire
à partir du cache data/raw/, puis substituée à l'ancienne. Une ingestion qui
échoue ne casse donc pas la base existante.

Chaque parseur est déclaré dans PARSERS avec le motif de fichiers qu'il traite
(parseurs dans sources.py). Issues possibles pour un fichier :
- ingéré ;
- ignoré (`Skip` : doublon AE, compte spécial, annexe non utilisée) ;
- à inspecter (`NotInspected` / `UnknownFormat` : format pas encore vu) ;
- erreur (exception inattendue).
Après ingestion : rapport de rapprochement (NON_RAPPROCHES.md) et de cohérence
(VALIDATION.md : contrôles croisés et couverture par exercice).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import duckdb

from budget_etat import db, nomenclature, sources
from budget_etat.fetch import RAW_DIR, ROOT, load_manifest
from budget_etat.jsonstat import decode
from budget_etat.sources import Skip, UnknownFormat

CORRESPONDANCES = ROOT / "data" / "correspondances.csv"
NON_RAPPROCHES = ROOT / "data" / "NON_RAPPROCHES.md"
VALIDATION = "VALIDATION.md"


class NotInspected(Exception):
    """Le parseur attend l'inspection du schéma réel du fichier."""


Row = tuple[str, dict]  # (table, valeurs)


@dataclass(frozen=True)
class Parser:
    name: str
    pattern: str  # glob relatif à data/raw
    fn: Callable[[Path], Iterator[Row]]


# --- Eurostat ---------------------------------------------------------------
# Codes de dimension Eurostat attendus. À confirmer dans data/INSPECTION.md :
# si un code manque, le parseur échoue en listant les codes présents.
EUROSTAT_SERIES = {
    "gov_10dd_edpt1": [
        # (filtre, indicateur canonique)
        ({"unit": "MIO_EUR", "sector": "S13", "na_item": "GD"}, "dette_maastricht"),
        ({"unit": "MIO_EUR", "sector": "S13", "na_item": "B9"}, "solde_maastricht"),
    ],
    "nama_10_gdp": [
        ({"unit": "CP_MEUR", "na_item": "B1GQ"}, "pib_nominal"),
    ],
}
PERIMETRE_MACRO = {"dette_maastricht": "APU", "solde_maastricht": "APU", "pib_nominal": "economie"}


def parse_eurostat(path: Path) -> Iterator[Row]:
    code = path.stem
    rows = decode(json.loads(path.read_text(encoding="utf-8")))
    for filt, indicateur in EUROSTAT_SERIES.get(code, []):
        sel = [r for r in rows if all(r.get(k) == v for k, v in filt.items())]
        if not sel:
            avail = {k: sorted({r[k] for r in rows}) for k in filt if rows and k in rows[0]}
            raise ValueError(f"{code}: aucune ligne pour {filt}. Codes présents : {avail}")
        for r in sel:
            if r.get("geo", "FR") != "FR" or not str(r["time"]).isdigit():
                continue
            yield "macro", dict(annee=int(r["time"]), indicateur=indicateur,
                                perimetre=PERIMETRE_MACRO[indicateur], valeur_meur=float(r["value"]),
                                source=f"Eurostat {code}")


def _pending(path: Path) -> Iterator[Row]:
    raise NotInspected(f"{path.parent.name}/{path.name} : schéma non inspecté, parseur à écrire")
    yield  # pragma: no cover


PARSERS: list[Parser] = [
    Parser("eurostat", "eurostat/*.json", parse_eurostat),
    Parser("insee", "insee/*.xml", sources.parse_insee),
    Parser("execution", "economie/execution-*/export.csv", sources.parse_exec_titres),
    Parser("execution", "economie/execution-*/attachments/*", sources.parse_exec_titres),
    Parser("plr", "economie/plr*/attachments/*", sources.parse_plr_attachment),
    Parser("plf_recettes", "economie/*recettes-du-budget-general/export.csv", sources.parse_plf_recettes),
    Parser("plf_depenses", "economie/*depenses*destination/export.csv", sources.parse_plf_depenses),
    # Tableaux mis en page (en-têtes sur 2 lignes) : à écrire après l'inspection complète.
    Parser("plf_recettes_nettes", "economie/plf-*-recettes-fiscales-nettes/attachments/*", _pending),
]


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    con.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [[r[c] for c in cols] for r in rows],
    )


def reconcile_nomenclature(con: duckdb.DuckDBPyConnection) -> list[nomenclature.Match]:
    progs = [nomenclature.Programme(*r) for r in con.execute(
        "SELECT DISTINCT exercice, programme_code, any_value(programme_lib), any_value(mission_lib) "
        "FROM depense WHERE programme_code IS NOT NULL GROUP BY 1, 2").fetchall()]
    matches = nomenclature.reconcile(progs, nomenclature.load_manual(CORRESPONDANCES))
    _insert(con, "nomenclature_programme", [m.__dict__ for m in matches])
    # Sources sans mission (exécution 2010, nomenclature ministère) : mission
    # déduite de la correspondance, marquée mission_code = 'déduite'.
    con.execute("""
        UPDATE depense d SET mission_lib = n.mission_canon, mission_code = 'déduite'
        FROM nomenclature_programme n
        WHERE d.mission_lib IS NULL AND n.exercice = d.exercice AND n.programme_code = d.programme_code
          AND n.mission_canon IS NOT NULL""")
    return matches


def write_unmatched_report(matches: list[nomenclature.Match], path: Path = NON_RAPPROCHES) -> None:
    lines = ["# Programmes non rapprochés ou à vérifier", "",
             "Ajouter les correspondances voulues dans `data/correspondances.csv` "
             "(exercice, programme_code, programme_canon, mission_canon, note) puis relancer `budget ingest`.", ""]
    for methode in ("non_rapproche", "code_seul"):
        sel = [m for m in matches if m.methode == methode]
        lines += [f"## {methode} ({len(sel)})", "", "| exercice | code | libellé | mission |", "|---|---|---|---|"]
        lines += [f"| {m.exercice} | {m.programme_code} | {m.programme_lib or ''} | {m.mission_lib or ''} |"
                  for m in sorted(sel, key=lambda m: (m.programme_code, m.exercice))]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_validation_report(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    """Contrôles croisés (montants publiés vs agrégation des tables) et couverture."""
    from budget_etat.queries import NET_FILTER

    lines = ["# Validation de l'ingestion", "", "## Contrôles croisés", "",
             "Écart = agrégation des tables canoniques − montant de référence publié (M€).", "",
             "| exercice | contrôle | référence | obtenu | écart | source |", "|---|---|---|---:|---:|---|"]
    rows = con.execute(f"""
        WITH ref AS (SELECT * FROM controle),
        dep AS (SELECT exercice, 'programme ' || programme_code AS cle, sum(montant_meur) AS v
                FROM depense WHERE nature = 'execution' GROUP BY 1, 2),
        rec AS (SELECT exercice, 'recettes ' || categorie AS cle, sum(montant_meur) AS v
                FROM recette WHERE nature = 'execution' GROUP BY 1, 2)
        SELECT r.exercice, r.cle, r.reference, coalesce(d.v, c.v) AS obtenu, r.source
        FROM ref r LEFT JOIN dep d USING (exercice, cle) LEFT JOIN rec c USING (exercice, cle)
        ORDER BY abs(coalesce(coalesce(d.v, c.v), 0) - r.reference) DESC, r.cle""").fetchall()
    n_ok = 0
    for ex, cle, ref, got, src in rows:
        ecart = None if got is None else got - ref
        if ecart is not None and abs(ecart) < 0.01:
            n_ok += 1
            continue
        lines.append(f"| {ex} | {cle} | {ref:,.2f} | {'–' if got is None else f'{got:,.2f}'} | "
                     f"{'absent' if ecart is None else f'{ecart:,.2f}'} | {src} |")
    lines += ["", f"{n_ok} contrôle(s) exact(s) à 0,01 M€ près (non listés), {len(rows) - n_ok} écart(s) listé(s).", ""]
    lines += ["## Couverture par exercice (exécution)", "",
              "Dépenses nettes = budget général hors remboursements et dégrèvements (programmes 200/201).", "",
              "| exercice | programmes | dépenses nettes | dont titre 4 | recettes nettes | dette État (déc.) | PIB |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    cov = con.execute(f"""
        WITH d AS (SELECT exercice, count(DISTINCT programme_code) np, sum(montant_meur) dep,
                          sum(CASE WHEN titre_code = '4' THEN montant_meur END) t4
                   FROM depense WHERE nature = 'execution' AND {NET_FILTER} GROUP BY 1),
        r AS (SELECT exercice, sum(CASE WHEN categorie = 'prelevement' THEN -montant_meur ELSE montant_meur END) rec
              FROM recette WHERE nature = 'execution' GROUP BY 1),
        a AS (SELECT exercice, max(montant_meur) FILTER (WHERE indicateur = 'dette_etat' AND mois = 12) dette
              FROM agregat_etat GROUP BY 1),
        m AS (SELECT annee AS exercice, max(valeur_meur) FILTER (WHERE indicateur = 'pib_nominal') pib
              FROM macro GROUP BY 1),
        y AS (SELECT exercice FROM d UNION SELECT exercice FROM r UNION SELECT exercice FROM a)
        SELECT y.exercice, np, dep, t4, rec, dette, pib FROM y LEFT JOIN d USING (exercice)
        LEFT JOIN r USING (exercice) LEFT JOIN a USING (exercice) LEFT JOIN m USING (exercice)
        ORDER BY 1""").fetchall()
    f = lambda v: "**manquant**" if v is None else f"{v:,.0f}"
    for ex, np_, dep, t4, rec, dette, pib in cov:
        lines.append(f"| {ex} | {np_ or '**0**'} | {f(dep)} | {f(t4)} | {f(rec)} | {f(dette)} | {f(pib)} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(raw_dir: Path = RAW_DIR, db_path: Path = db.DB_PATH, parsers: list[Parser] | None = None) -> int:
    parsers = PARSERS if parsers is None else parsers
    manifest = load_manifest() if raw_dir == RAW_DIR else {}
    by_path = {v["path"]: v for v in manifest.values() if "path" in v}
    tmp = db_path.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = db.connect(tmp)
    pending, errors, done, skipped = [], [], 0, 0
    try:
        for p in parsers:
            for f in sorted(raw_dir.glob(p.pattern)):
                if f.name.startswith("_") or f.name.endswith(".part"):
                    continue
                try:
                    buf: dict[str, list[dict]] = {}
                    for table, row in p.fn(f):
                        buf.setdefault(table, []).append(row)
                except Skip:
                    skipped += 1
                    continue
                except (NotInspected, UnknownFormat) as e:
                    pending.append(f"{p.name} {f.parent.name}/{f.name} : {e}"[:400])
                    continue
                except Exception as e:
                    errors.append(f"{p.name} {f.name}: {type(e).__name__}: {e}")
                    continue
                for table, rows in buf.items():
                    _insert(con, table, rows)
                rel = str(f.relative_to(raw_dir))
                m = by_path.get(rel, {})
                con.execute("INSERT INTO source_file VALUES (?, ?, ?, ?, ?)",
                            [rel, m.get("url"), m.get("fetched_at"), m.get("sha256"), p.name])
                done += 1
                print(f"✓ {p.name} {rel} : " + ", ".join(f"{t}={len(r)}" for t, r in buf.items()))
        matches = reconcile_nomenclature(con)
        if matches:
            write_unmatched_report(matches, db_path.parent / NON_RAPPROCHES.name)
        write_validation_report(con, db_path.parent / VALIDATION)
        con.close()
        tmp.replace(db_path)
    finally:
        tmp.unlink(missing_ok=True)
    for msg in pending:
        print(f"… {msg}")
    for msg in errors:
        print(f"ERREUR {msg}")
    print(f"{done} fichier(s) ingéré(s), {skipped} ignoré(s), {len(pending)} à inspecter, "
          f"{len(errors)} erreur(s). Base : {db_path}")
    return 1 if errors else 0
