"""Profilage des fichiers bruts téléchargés -> data/INSPECTION.md.

Sert à écrire les parseurs d'`ingest.py` à partir des schémas RÉELS : pour
chaque fichier tabulaire on liste les colonnes, types détectés, taux de vide,
cardinalité, min/max, valeurs les plus fréquentes et quelques lignes d'exemple.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from budget_etat.fetch import RAW_DIR, ROOT

REPORT = ROOT / "data" / "INSPECTION.md"
LOW_CARD = 60
TOP = 25


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _open_csv(con: duckdb.DuckDBPyConnection, path: Path) -> str:
    """Crée une vue `t` sur le CSV ; essaie UTF-8 puis latin-1. Retourne l'encodage."""
    last = None
    for enc in ("utf-8", "latin-1"):
        try:
            con.execute(
                f"CREATE OR REPLACE VIEW t AS SELECT * FROM read_csv('{str(path).replace(chr(39), chr(39) * 2)}', "
                f"encoding='{enc}', sample_size=-1, null_padding=true)"
            )
            con.execute("SELECT count(*) FROM t").fetchone()
            return enc
        except duckdb.Error as e:  # encodage ou dialecte non reconnu
            last = e
    raise last  # type: ignore[misc]


def profile_table(con: duckdb.DuckDBPyConnection) -> list[str]:
    out: list[str] = []
    n = con.execute("SELECT count(*) FROM t").fetchone()[0]
    cols = con.execute("DESCRIBE t").fetchall()
    out.append(f"{n} lignes, {len(cols)} colonnes\n")
    out.append("| colonne | type | vides | distincts | min | max |")
    out.append("|---|---|---|---|---|---|")
    low_card = []
    for name, typ, *_ in cols:
        c = _q(name)
        nulls, distinct, lo, hi = con.execute(
            f"SELECT count(*) - count({c}), count(DISTINCT {c}), min({c})::VARCHAR, max({c})::VARCHAR FROM t"
        ).fetchone()
        cell = lambda v: (str(v)[:40] if v is not None else "").replace("|", "\\|").replace("\n", " ")
        out.append(f"| `{name}` | {typ} | {nulls} | {distinct} | {cell(lo)} | {cell(hi)} |")
        if 0 < distinct <= LOW_CARD:
            low_card.append(name)
    for name in low_card:
        rows = con.execute(
            f"SELECT {_q(name)}::VARCHAR AS v, count(*) AS n FROM t GROUP BY 1 ORDER BY n DESC, v LIMIT {TOP}"
        ).fetchall()
        vals = ", ".join(f"`{v}` ({k})" for v, k in rows)
        out.append(f"\n- **`{name}`** : {vals}")
    sample = con.execute("SELECT * FROM t LIMIT 5").fetchall()
    out.append("\nExemple (5 premières lignes) :\n```")
    out.append(" ; ".join(c[0] for c in cols))
    for r in sample:
        out.append(" ; ".join("" if v is None else str(v) for v in r))
    out.append("```")
    return out


def describe_json(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))

    def shape(x, depth=0):
        if depth > 3:
            return "…"
        if isinstance(x, dict):
            return "{" + ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in list(x.items())[:25]) + "}"
        if isinstance(x, list):
            return f"[{len(x)} × {shape(x[0], depth + 1) if x else '∅'}]"
        return type(x).__name__

    return ["```", shape(data)[:4000], "```"]


def run(raw_dir: Path = RAW_DIR, report: Path = REPORT) -> int:
    files = sorted(p for p in raw_dir.rglob("*") if p.is_file() and not p.name.endswith(".part")
                   and p.name != "manifest.json")
    if not files:
        print(f"Aucun fichier dans {raw_dir} : lancer d'abord `budget fetch`.")
        return 1
    lines = ["# Inspection des fichiers bruts", "",
             "Généré par `budget inspect`. À partager pour écrire les parseurs d'ingestion.", ""]
    con = duckdb.connect()
    for p in files:
        rel = p.relative_to(raw_dir)
        lines += [f"## `{rel}`", f"{p.stat().st_size} octets", ""]
        suffix = p.suffix.lower()
        try:
            if suffix in (".csv", ".txt") or (suffix == "" and p.read_bytes()[:1] not in (b"{", b"[")):
                enc = _open_csv(con, p)
                lines.append(f"encodage : {enc}")
                lines += profile_table(con)
            elif suffix == ".json" or p.read_bytes()[:1] in (b"{", b"["):
                lines += describe_json(p)
            else:
                lines.append(f"(format {suffix or 'inconnu'} non profilé)")
        except Exception as e:  # on documente l'échec plutôt que d'interrompre
            lines.append(f"⚠ échec du profilage : {type(e).__name__}: {e}")
        lines.append("")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"Rapport écrit : {report}")
    return 0
