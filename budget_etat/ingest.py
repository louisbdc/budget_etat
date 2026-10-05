"""Normalisation des fichiers bruts vers la base canonique (data/budget.duckdb).

Idempotent : la base est reconstruite intégralement dans un fichier temporaire
à partir du cache data/raw/, puis substituée à l'ancienne. Une ingestion qui
échoue ne casse donc pas la base existante.

Chaque parseur est déclaré dans PARSERS avec le motif de fichiers qu'il traite.
Les parseurs des sources DGFiP / data.economie ne sont PAS encore écrits : leurs
schémas n'ont pas pu être inspectés (cf. README). Ils lèvent NotInspected.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import duckdb

from budget_etat import db, nomenclature
from budget_etat.fetch import RAW_DIR, ROOT, load_manifest
from budget_etat.jsonstat import decode

CORRESPONDANCES = ROOT / "data" / "correspondances.csv"
NON_RAPPROCHES = ROOT / "data" / "NON_RAPPROCHES.md"


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


# --- DGFiP / data.economie : en attente d'inspection -------------------------
def _pending(path: Path) -> Iterator[Row]:
    raise NotInspected(f"{path.parent.name}/{path.name} : schéma non inspecté, parseur à écrire")
    yield  # pragma: no cover


PARSERS: list[Parser] = [
    Parser("eurostat", "eurostat/*.json", parse_eurostat),
    Parser("dgfip_sme", "dgfip_sme/*.csv", _pending),
    Parser("economie_sme", "economie/situation-mensuelle-de-l-etat/export.csv", _pending),
    Parser("economie_plrg", "economie/plrg-*/attachments/*", _pending),
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


def run(raw_dir: Path = RAW_DIR, db_path: Path = db.DB_PATH, parsers: list[Parser] | None = None) -> int:
    parsers = PARSERS if parsers is None else parsers
    manifest = load_manifest() if raw_dir == RAW_DIR else {}
    by_path = {v["path"]: v for v in manifest.values() if "path" in v}
    tmp = db_path.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = db.connect(tmp)
    pending, errors, done = [], [], 0
    try:
        for p in parsers:
            for f in sorted(raw_dir.glob(p.pattern)):
                if f.name.startswith("_") or f.name.endswith(".part"):
                    continue
                try:
                    buf: dict[str, list[dict]] = {}
                    for table, row in p.fn(f):
                        buf.setdefault(table, []).append(row)
                except NotInspected as e:
                    pending.append(str(e))
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
        con.close()
        tmp.replace(db_path)
    finally:
        tmp.unlink(missing_ok=True)
    for msg in pending:
        print(f"… {msg}")
    for msg in errors:
        print(f"ERREUR {msg}")
    print(f"{done} fichier(s) ingéré(s), {len(pending)} en attente d'inspection, {len(errors)} erreur(s). Base : {db_path}")
    return 1 if errors else 0
