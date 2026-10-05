"""Récupération des sources brutes avec cache local et manifeste.

Étape 1 de l'ingestion : on télécharge les fichiers tels quels dans
`data/raw/<source>/` et on consigne dans `data/raw/manifest.json` l'URL, la
date de récupération, la taille et le sha256 de chaque fichier. Relancer la
commande ne retélécharge pas un fichier inchangé (comparaison de la date de
modification annoncée par la source).

La normalisation (étape 2, `ingest.py`) ne suppose aucun schéma : les parseurs
s'écrivent à partir du rapport produit par `budget inspect`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
MANIFEST = RAW_DIR / "manifest.json"

# --- data.gouv.fr (API catalogue v1) : toutes les ressources du jeu ----------
DATAGOUV_DATASETS = {
    "dgfip_sme": "dgfip-situation-mensuelle-de-letat",
}

# --- data.economie.gouv.fr (Opendatasoft, API explore v2.1) ------------------
ODS_BASE = "https://data.economie.gouv.fr/api/explore/v2.1"
# Identifiants repérés par recherche web (non vérifiés faute d'accès réseau) :
ODS_DATASETS = [
    "situation-mensuelle-de-l-etat",  # miroir ODS de la SME DGFiP
    "plrg-2024",  # projet de loi relative aux résultats et à la gestion 2024 (annexes CSV en pièces jointes)
    "plf-2024-recettes-du-budget-general",
    "plf25-depenses-2025-selon-destination",
]
# Recherches au catalogue : le résultat est sauvegardé, et les jeux dont
# l'identifiant correspond à ODS_AUTO_PATTERNS sont aussi téléchargés.
ODS_QUERIES = ["situation mensuelle", "loi de règlement", "résultats et gestion", "exécution budget",
               "recettes du budget général", "recettes fiscales", "loi de finances initiale"]
ODS_AUTO_PATTERNS = [r"^plrg?-?\d{2,4}", r"^execution-\d{4}", r"recettes"]
ODS_MAX_AUTO = 40  # garde-fou

# --- Eurostat (JSON-stat) : agrégats APU au sens de Maastricht et PIB --------
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
EUROSTAT_DATASETS = {
    "gov_10dd_edpt1": {"geo": "FR", "freq": "A"},  # déficit / dette APU (notification PDE)
    "nama_10_gdp": {"geo": "FR", "freq": "A", "na_item": "B1GQ", "unit": "CP_MEUR"},  # PIB nominal
}

TIMEOUT = httpx.Timeout(120.0, connect=20.0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return {}


def _save_manifest(m: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False, sort_keys=True))
    tmp.replace(MANIFEST)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:80]


def _download(client: httpx.Client, url: str, dest: Path, params: dict | None = None) -> dict:
    """Télécharge vers un fichier temporaire puis renomme (pas de fichier tronqué)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    with client.stream("GET", url, params=params, follow_redirects=True) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
                h.update(chunk)
    tmp.replace(dest)
    return {"url": str(r.url), "path": str(dest.relative_to(RAW_DIR)), "sha256": h.hexdigest(),
            "bytes": dest.stat().st_size, "fetched_at": _now()}


def _cached(manifest: dict, key: str, stamp: str | None) -> bool:
    prev = manifest.get(key)
    return bool(stamp and prev and prev.get("source_stamp") == stamp and (RAW_DIR / prev["path"]).exists())


def fetch_datagouv(client: httpx.Client, manifest: dict) -> None:
    for name, slug in DATAGOUV_DATASETS.items():
        meta_url = f"https://www.data.gouv.fr/api/1/datasets/{slug}/"
        meta = client.get(meta_url, follow_redirects=True).raise_for_status().json()
        (RAW_DIR / name).mkdir(parents=True, exist_ok=True)
        (RAW_DIR / name / "_dataset.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        for res in meta.get("resources", []):
            key = f"{name}/{res['id']}"
            stamp = res.get("last_modified") or (res.get("checksum") or {}).get("value")
            if _cached(manifest, key, stamp):
                print(f"= {key} inchangé")
                continue
            fname = Path(res["url"].split("?")[0]).name or res["id"]
            entry = _download(client, res["url"], RAW_DIR / name / f"{res['id'][:8]}_{fname}")
            entry.update(title=res.get("title"), format=res.get("format"), source_stamp=stamp, dataset=meta_url)
            manifest[key] = entry
            print(f"+ {key} ({entry['bytes']} o)")
            _save_manifest(manifest)


def _ods_dataset(client: httpx.Client, manifest: dict, ds: str) -> None:
    """Métadonnées (dont la liste des champs), export CSV complet et pièces jointes."""
    d = RAW_DIR / "economie" / ds
    d.mkdir(parents=True, exist_ok=True)
    meta = client.get(f"{ODS_BASE}/catalog/datasets/{ds}", follow_redirects=True).raise_for_status().json()
    (d / "_dataset.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    metas = (meta.get("metas") or {}).get("default") or {}
    stamp = metas.get("data_processed") or metas.get("modified")
    key = f"economie/{ds}/export.csv"
    if _cached(manifest, key, stamp):
        print(f"= {key} inchangé")
    else:
        entry = _download(client, f"{ODS_BASE}/catalog/datasets/{ds}/exports/csv", d / "export.csv",
                          params={"delimiter": ";", "with_bom": "false"})
        entry.update(title=metas.get("title"), source_stamp=stamp, dataset=f"{ODS_BASE}/catalog/datasets/{ds}")
        manifest[key] = entry
        print(f"+ {key} ({entry['bytes']} o)")
    att = client.get(f"{ODS_BASE}/catalog/datasets/{ds}/attachments", follow_redirects=True)
    if att.status_code == 200:
        (d / "_attachments.json").write_text(att.text)
        for a in att.json().get("attachments", []):
            href = next((l["href"] for l in a.get("links", []) if l.get("rel") == "self"), a.get("href"))
            if not href:
                continue
            name = a.get("id") or Path(href).name
            key = f"economie/{ds}/attachments/{name}"
            if _cached(manifest, key, stamp):
                continue
            entry = _download(client, href, d / "attachments" / _slug(name))
            entry.update(title=a.get("title"), source_stamp=stamp)
            manifest[key] = entry
            print(f"+ {key} ({entry['bytes']} o)")
    _save_manifest(manifest)


def fetch_economie(client: httpx.Client, manifest: dict) -> None:
    found: list[str] = []
    for q in ODS_QUERIES:
        entry = _download(client, f"{ODS_BASE}/catalog/datasets", RAW_DIR / "economie" / f"_catalog_{_slug(q)}.json",
                          params={"where": f'search("{q}")', "limit": 100, "select": "dataset_id"})
        manifest[f"economie/_catalog/{_slug(q)}"] = entry
        res = json.loads((RAW_DIR / entry["path"]).read_text()).get("results", [])
        found += [r["dataset_id"] for r in res if any(re.search(p, r["dataset_id"]) for p in ODS_AUTO_PATTERNS)]
    _save_manifest(manifest)
    auto = [d for d in dict.fromkeys(found) if d not in ODS_DATASETS][:ODS_MAX_AUTO]
    for ds in ODS_DATASETS + auto:
        try:
            _ods_dataset(client, manifest, ds)
        except httpx.HTTPStatusError as e:
            print(f"! economie/{ds} : HTTP {e.response.status_code}")


def fetch_eurostat(client: httpx.Client, manifest: dict) -> None:
    for code, params in EUROSTAT_DATASETS.items():
        entry = _download(client, f"{EUROSTAT}/{code}", RAW_DIR / "eurostat" / f"{code}.json",
                          params={**params, "format": "JSON", "lang": "fr"})
        manifest[f"eurostat/{code}"] = entry
        print(f"+ eurostat {code}")
    _save_manifest(manifest)


def run() -> int:
    manifest = load_manifest()
    errors = []
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "budget-etat (usage perso)"}) as client:
        for step in (fetch_datagouv, fetch_economie, fetch_eurostat):
            try:
                step(client, manifest)
            except httpx.HTTPError as e:
                # Pas de repli sur des données fictives : on signale et on continue.
                errors.append(f"{step.__name__}: {type(e).__name__}: {e}")
    for e in errors:
        print(f"ERREUR {e}")
    return 1 if errors else 0
