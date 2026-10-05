"""Récupération des sources brutes avec cache local et manifeste.

Étape 1 de l'ingestion : on télécharge les fichiers tels quels dans
`data/raw/<source>/` et on consigne dans `data/raw/manifest.json` l'URL, la
date de récupération, la taille et le sha256 de chaque fichier. Relancer la
commande ne retélécharge pas un fichier inchangé (comparaison de la date de
modification / du checksum annoncés par la source).

La normalisation (étape 2) ne sera écrite qu'après inspection des fichiers
réels : aucun schéma n'est supposé ici.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
MANIFEST = RAW_DIR / "manifest.json"

# Jeux data.gouv.fr récupérés en entier (API catalogue v1 : /api/1/datasets/<slug>/).
DATAGOUV_DATASETS = {
    "dgfip_sme": "dgfip-situation-mensuelle-de-letat",
}

# Recherches au catalogue data.economie.gouv.fr (Opendatasoft, API explore v2.1).
# Le résultat est sauvegardé pour choisir les jeux à ingérer après inspection.
ECONOMIE_CATALOG = "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets"
ECONOMIE_QUERIES = ["budget de l'etat", "execution mission programme", "recettes fiscales", "comptes publics"]

# Eurostat (API de diffusion, JSON-stat) : agrégats APU au sens de Maastricht.
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
EUROSTAT_DATASETS = {
    # Déficit/dette APU (notification PDE) ; contient aussi le PIB nominal.
    "gov_10dd_edpt1": {"geo": "FR", "freq": "A"},
}

TIMEOUT = httpx.Timeout(60.0, connect=20.0)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return {}


def _save_manifest(m: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False, sort_keys=True))
    tmp.replace(MANIFEST)


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


def fetch_datagouv(client: httpx.Client, manifest: dict) -> None:
    for name, slug in DATAGOUV_DATASETS.items():
        meta_url = f"https://www.data.gouv.fr/api/1/datasets/{slug}/"
        meta = client.get(meta_url, follow_redirects=True).raise_for_status().json()
        (RAW_DIR / name).mkdir(parents=True, exist_ok=True)
        (RAW_DIR / name / "_dataset.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        for res in meta.get("resources", []):
            key = f"{name}/{res['id']}"
            stamp = res.get("last_modified") or (res.get("checksum") or {}).get("value")
            prev = manifest.get(key)
            if prev and prev.get("source_stamp") == stamp and (RAW_DIR / prev["path"]).exists():
                print(f"= {key} inchangé")
                continue
            fname = Path(res["url"].split("?")[0]).name or res["id"]
            entry = _download(client, res["url"], RAW_DIR / name / f"{res['id'][:8]}_{fname}")
            entry.update(title=res.get("title"), format=res.get("format"), source_stamp=stamp, dataset=meta_url)
            manifest[key] = entry
            print(f"+ {key} ({entry['bytes']} o)")
            _save_manifest(manifest)


def fetch_economie_catalog(client: httpx.Client, manifest: dict) -> None:
    for q in ECONOMIE_QUERIES:
        slug = "".join(c if c.isalnum() else "_" for c in q)
        entry = _download(client, ECONOMIE_CATALOG, RAW_DIR / "economie" / f"catalog_{slug}.json",
                          params={"where": f'search("{q}")', "limit": 100})
        manifest[f"economie/catalog/{slug}"] = entry
        print(f"+ catalogue « {q} »")
    _save_manifest(manifest)


def fetch_eurostat(client: httpx.Client, manifest: dict) -> None:
    for code, params in EUROSTAT_DATASETS.items():
        entry = _download(client, f"{EUROSTAT}/{code}", RAW_DIR / "eurostat" / f"{code}.json",
                          params={**params, "format": "JSON", "lang": "fr"})
        manifest[f"eurostat/{code}"] = entry
        print(f"+ eurostat {code}")
    _save_manifest(manifest)


def run() -> int:
    manifest = _load_manifest()
    errors = []
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "budget-etat (usage perso)"}) as client:
        for step in (fetch_datagouv, fetch_economie_catalog, fetch_eurostat):
            try:
                step(client, manifest)
            except httpx.HTTPError as e:
                # Pas de repli sur des données fictives : on signale et on continue.
                errors.append(f"{step.__name__}: {type(e).__name__}: {e}")
    for e in errors:
        print(f"ERREUR {e}")
    return 1 if errors else 0
