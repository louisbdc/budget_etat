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
# Jeux toujours téléchargés (identifiants confirmés par la 1re inspection).
ODS_DATASETS = [
    "plrg-2024",  # résultats de la gestion 2024 : annexes CSV en pièces jointes
    "plf-2024-recettes-du-budget-general",
    "plf25-recettes-du-budget-general",
    "plf25-depenses-2025-selon-destination",
]
# Le catalogue complet est listé (data/raw/economie/_catalog_all.json) puis filtré
# sur identifiant + titre (sans accents, minuscules). Inclusion si UN motif
# correspond, exclusion si UN motif d'exclusion correspond.
ODS_INCLUDE = [
    r"^plrg?-\d{4}",  # lois de règlement / résultats de la gestion
    r"loi de reglement|resultats de la gestion",
    r"^execution-\d{4}-du-budget-(general|de-letat)",
    r"recettes fiscales nettes",
    r"recettes du budget general",
    r"depenses .*selon destination",
    r"situations mensuelles budgetaires",  # SME en données, 2013 à nos jours
]
# Attention : « approbation-des-comptes-de-lannee » (PLRG) ne doit pas être exclu.
ODS_EXCLUDE = [r"performance", r"budget vert", r"-en-ae$", r"comptes-daffectation", r"comptes-de-concours",
               r"ministere-progr"]
ODS_MAX_AUTO = 120  # garde-fou

# --- INSEE (BDM) : dette négociable de l'État -------------------------------
# La page de la famille BDM liste les identifiants de séries ; on les télécharge
# ensuite via l'API SDMX publique. 001739081 (encours total) a été identifié à
# la 2e inspection ; c'est la seule série retenue comme dette de l'État.
INSEE_FAMILLES = {"dette_negociable": "https://www.insee.fr/fr/plan-du-site/famille-bdm/102765717"}
INSEE_SERIES_CONNUES = ["001739081"]
INSEE_SDMX = "https://bdm.insee.fr/series/sdmx/data/SERIES_BDM"

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


def _norm(s: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def select_datasets(catalog: list[dict]) -> list[str]:
    """Filtre le catalogue sur identifiant + titre (fonction pure, testée)."""
    out = []
    for d in catalog:
        text = f"{d['id']} {_norm(d.get('title', ''))}"
        if any(re.search(p, d["id"]) or re.search(p, text) for p in ODS_INCLUDE) and \
                not any(re.search(p, d["id"]) or re.search(p, text) for p in ODS_EXCLUDE):
            out.append(d["id"])
    return out


def fetch_economie(client: httpx.Client, manifest: dict) -> None:
    catalog: list[dict] = []
    offset = 0
    while True:
        r = client.get(f"{ODS_BASE}/catalog/datasets", params={"limit": 100, "offset": offset},
                       follow_redirects=True).raise_for_status().json()
        for d in r.get("results", []):
            meta = (d.get("metas") or {}).get("default") or {}
            catalog.append({"id": d["dataset_id"], "title": meta.get("title"), "modified": meta.get("modified")})
        offset += 100
        if offset >= r.get("total_count", 0) or not r.get("results"):
            break
    dest = RAW_DIR / "economie" / "_catalog_all.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(catalog, indent=1, ensure_ascii=False))
    print(f"+ catalogue data.economie : {len(catalog)} jeux")
    auto = [d for d in select_datasets(catalog) if d not in ODS_DATASETS][:ODS_MAX_AUTO]
    print(f"  {len(auto)} jeux sélectionnés automatiquement")
    for ds in ODS_DATASETS + auto:
        try:
            _ods_dataset(client, manifest, ds)
        except httpx.HTTPStatusError as e:
            print(f"! economie/{ds} : HTTP {e.response.status_code}")


def fetch_insee(client: httpx.Client, manifest: dict) -> None:
    ids = list(INSEE_SERIES_CONNUES)
    for name, url in INSEE_FAMILLES.items():
        r = client.get(url, follow_redirects=True)
        if r.status_code == 200:
            (RAW_DIR / "insee").mkdir(parents=True, exist_ok=True)
            (RAW_DIR / "insee" / f"_famille_{name}.html").write_text(r.text)
            ids += re.findall(r"/statistiques/serie/(\d{9})", r.text)
        else:
            print(f"! INSEE famille {name} : HTTP {r.status_code}")
    ids = list(dict.fromkeys(ids))
    entry = _download(client, f"{INSEE_SDMX}/{'+'.join(ids)}", RAW_DIR / "insee" / "dette_negociable.xml")
    entry["series"] = ids
    manifest["insee/dette_negociable"] = entry
    print(f"+ INSEE : {len(ids)} séries")
    _save_manifest(manifest)


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
        for step in (fetch_datagouv, fetch_economie, fetch_insee, fetch_eurostat):
            try:
                step(client, manifest)
            except httpx.HTTPError as e:
                # Pas de repli sur des données fictives : on signale et on continue.
                errors.append(f"{step.__name__}: {type(e).__name__}: {e}")
    for e in errors:
        print(f"ERREUR {e}")
    return 1 if errors else 0
