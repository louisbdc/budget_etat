"""Décodage du format JSON-stat 2.0 (API Eurostat) en lignes « longues ».

Le format est un standard public (https://json-stat.org/full/) : `id` liste les
dimensions, `size` leurs tailles, `dimension.<d>.category.index` la position
de chaque modalité, et `value` est un tableau ou un dict {index_aplati: valeur}
en ordre row-major (la dernière dimension varie le plus vite).
"""

from __future__ import annotations

from itertools import product


def _categories(dim: dict) -> list[str]:
    idx = dim["category"]["index"]
    if isinstance(idx, list):
        return idx
    return [k for k, _ in sorted(idx.items(), key=lambda kv: kv[1])]


def decode(ds: dict) -> list[dict]:
    ids: list[str] = ds["id"]
    cats = [_categories(ds["dimension"][d]) for d in ids]
    values = ds.get("value", {})
    get = (lambda i: values[i] if i < len(values) else None) if isinstance(values, list) else (lambda i: values.get(str(i)))
    rows = []
    for flat, combo in enumerate(product(*cats)):
        v = get(flat)
        if v is None:
            continue
        row = dict(zip(ids, combo))
        row["value"] = v
        rows.append(row)
    return rows
