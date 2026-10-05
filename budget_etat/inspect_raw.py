"""Profilage des fichiers bruts téléchargés -> data/INSPECTION.md.

Sert à écrire les parseurs d'`ingest.py` à partir des schémas RÉELS : pour
chaque fichier tabulaire on liste les colonnes, types détectés, taux de vide,
cardinalité, min/max, valeurs les plus fréquentes et quelques lignes d'exemple.
Cas particuliers : archives zip (contenu profilé), catalogue data.economie
(liste id/titre), JSON-stat Eurostat (codes de dimensions), SDMX INSEE (séries).
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

import duckdb

from budget_etat.fetch import RAW_DIR, ROOT
from budget_etat.sources import decode_bytes

REPORT = ROOT / "data" / "INSPECTION.md"
LOW_CARD = 60
TOP = 25


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _open_csv(con: duckdb.DuckDBPyConnection, data: bytes, tmpdir: Path) -> str:
    """Décode en Python (utf-8 / cp1252 / latin-1), réécrit en UTF-8, crée la vue `t`."""
    text, enc = decode_bytes(data)
    tmp = tmpdir / "f.csv"
    tmp.write_text(text, encoding="utf-8")
    con.execute(f"CREATE OR REPLACE VIEW t AS SELECT * FROM read_csv('{tmp}', sample_size=-1, null_padding=true)")
    con.execute("SELECT count(*) FROM t").fetchone()
    return enc


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
    limit = n if n <= 120 else 12  # petit fichier : on montre tout
    sample = con.execute(f"SELECT * FROM t LIMIT {limit}").fetchall()
    out.append(f"\nExemple ({limit} premières lignes) :\n```")
    out.append(" ; ".join(c[0] for c in cols))
    for r in sample:
        out.append(" ; ".join("" if v is None else str(v) for v in r))
    out.append("```")
    return out


def describe_json(data) -> list[str]:
    def shape(x, depth=0):
        if depth > 3:
            return "…"
        if isinstance(x, dict):
            return "{" + ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in list(x.items())[:25]) + "}"
        if isinstance(x, list):
            return f"[{len(x)} × {shape(x[0], depth + 1) if x else '∅'}]"
        return type(x).__name__

    return ["```", shape(data)[:4000], "```"]


def describe_jsonstat(ds: dict) -> list[str]:
    out = [f"JSON-stat : {ds.get('label')} — mis à jour {ds.get('updated')}", ""]
    for d in ds["id"]:
        cat = ds["dimension"][d]["category"]
        labels = cat.get("label", {})
        idx = cat["index"]
        codes = idx if isinstance(idx, list) else sorted(idx, key=idx.get)
        if d == "time":
            out.append(f"- `time` : {codes[0]} → {codes[-1]} ({len(codes)} périodes)")
        else:
            out.append(f"- `{d}` : " + ", ".join(f"`{c}` ({labels.get(c, '')})" for c in codes[:40]))
    return out


def describe_catalog(cat: list[dict]) -> list[str]:
    out = [f"{len(cat)} jeux au catalogue", "", "| id | titre |", "|---|---|"]
    out += [f"| `{d['id']}` | {(d.get('title') or '').replace('|', '/')} |" for d in sorted(cat, key=lambda d: d["id"])]
    return out


def describe_sdmx(data: bytes) -> list[str]:
    from budget_etat.sources import parse_sdmx

    out = []
    for s in parse_sdmx(data):
        obs = s["obs"]
        attrs = {k: v for k, v in s["attrs"].items() if k not in ("TITLE_EN",)}
        out.append(f"- **{attrs.get('IDBANK')}** : {attrs.get('TITLE_FR')}")
        out.append(f"  - attributs : {json.dumps(attrs, ensure_ascii=False)[:600]}")
        if obs:
            out.append(f"  - {len(obs)} obs., {obs[0]['TIME_PERIOD']} → {obs[-1]['TIME_PERIOD']} ; "
                       f"exemple : {json.dumps(obs[-1], ensure_ascii=False)}")
    return out or ["(aucune série SDMX trouvée)"]


def describe_workbook(data: bytes, tmpdir: Path) -> list[str]:
    from budget_etat.sources import read_sheets

    tmp = tmpdir / "classeur"
    tmp.write_bytes(data)
    out = []
    for name, grid in read_sheets(tmp):
        width = max((len(r) for r in grid), default=0)
        out.append(f"\n### feuille `{name}` — {len(grid)} lignes × {width} colonnes\n")
        limit = len(grid) if len(grid) <= 120 else 25
        out.append(f"{limit} premières lignes :\n```")
        out += [" ; ".join(r).rstrip(" ;")[:600] for r in grid[:limit]]
        out.append("```")
    return out


def describe_pdf(data: bytes) -> list[str]:
    """Texte de la 1re et de la dernière page + toutes les lignes de note (« * »)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [p.extract_text() or "" for p in reader.pages]
    notes = [l.strip() for t in pages for l in t.splitlines() if l.strip().startswith("*") or "**" in l]
    out = [f"PDF, {len(pages)} pages. Début :", "```", pages[0][:1500] if pages else "", "```"]
    if notes:
        out += ["Notes (lignes contenant « * ») :", "```", "\n".join(dict.fromkeys(notes))[:2000], "```"]
    return out


def _profile_bytes(con, name: str, data: bytes, tmpdir: Path) -> list[str]:
    suffix = Path(name).suffix.lower()
    head = data.lstrip()[:1]
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return describe_workbook(data, tmpdir)
    if data[:5] == b"%PDF-":
        return describe_pdf(data)
    if zipfile.is_zipfile(io.BytesIO(data)) and "[Content_Types].xml" in zipfile.ZipFile(io.BytesIO(data)).namelist():
        return describe_workbook(data, tmpdir)
    if zipfile.is_zipfile(io.BytesIO(data)):
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                out.append(f"\n### (zip) `{info.filename}` — {info.file_size} octets\n")
                if info.is_dir():
                    continue
                inner = z.read(info)
                if Path(info.filename).suffix.lower() in (".txt", ".md"):
                    out += ["```", decode_bytes(inner)[0][:3000], "```"]
                else:
                    out += _profile_bytes(con, info.filename, inner, tmpdir)
        return out
    if head == b"<":
        return describe_sdmx(data)
    if suffix == ".json" or head in (b"{", b"["):
        obj = json.loads(decode_bytes(data)[0])
        if isinstance(obj, dict) and "dimension" in obj and "id" in obj:
            return describe_jsonstat(obj)
        if name.endswith("_catalog_all.json"):
            return describe_catalog(obj)
        return describe_json(obj)
    if suffix in (".docx", ".odt", ".ods"):
        return [f"(format {suffix} non profilé)"]
    try:
        enc = _open_csv(con, data, tmpdir)
        return [f"encodage : {enc}"] + profile_table(con)
    except duckdb.Error as e:
        # Dialecte non détecté par DuckDB : lecture Python brute des premières lignes.
        out = [f"(profilage DuckDB impossible : {str(e).splitlines()[0][:200]})"]
        return out + describe_workbook(data, tmpdir)


def run(raw_dir: Path = RAW_DIR, report: Path = REPORT) -> int:
    files = sorted(p for p in raw_dir.rglob("*") if p.is_file() and not p.name.endswith(".part")
                   and p.name != "manifest.json" and not p.name.startswith("_famille"))
    if not files:
        print(f"Aucun fichier dans {raw_dir} : lancer d'abord `budget fetch`.")
        return 1
    lines = ["# Inspection des fichiers bruts", "",
             "Généré par `budget inspect`. À partager pour écrire les parseurs d'ingestion.", ""]
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as td:
        for p in files:
            rel = p.relative_to(raw_dir)
            if p.name in ("_dataset.json", "_attachments.json") or p.name.startswith("_catalog_") and \
                    p.name != "_catalog_all.json":
                continue  # métadonnées volumineuses sans intérêt pour les parseurs
            lines += [f"## `{rel}`", f"{p.stat().st_size} octets", ""]
            try:
                lines += _profile_bytes(con, p.name, p.read_bytes(), Path(td))
            except Exception as e:  # on documente l'échec plutôt que d'interrompre
                lines.append(f"⚠ échec du profilage : {type(e).__name__}: {str(e)[:500]}")
            lines.append("")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"Rapport écrit : {report}")
    return 0
