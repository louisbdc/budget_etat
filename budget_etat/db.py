"""Schéma canonique DuckDB (montants en M€ courants).

Les parseurs de sources produisent des lignes dans ces tables ; le front et le
simulateur ne lisent QUE ces tables, jamais les fichiers bruts.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from budget_etat.fetch import ROOT

DB_PATH = ROOT / "data" / "budget.duckdb"

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_file (
    path VARCHAR PRIMARY KEY, url VARCHAR, fetched_at VARCHAR, sha256 VARCHAR, parser VARCHAR
);

-- Dépenses du budget général. periode : 'annuel' (exécution de l'exercice),
-- 'cumul_mensuel' (situation à fin de mois : cumul pour un flux, encours pour un
-- stock). nature : 'execution', 'lfi', 'plf'. Le programme 200/201
-- (remboursements et dégrèvements) est stocké mais exclu des vues « nettes ».
-- titre_* est NULL quand la source ne descend pas au titre.
CREATE TABLE IF NOT EXISTS depense (
    exercice INTEGER NOT NULL, mois INTEGER, periode VARCHAR NOT NULL, nature VARCHAR NOT NULL,
    mission_code VARCHAR, mission_lib VARCHAR,
    programme_code VARCHAR, programme_lib VARCHAR,
    titre_code VARCHAR, titre_lib VARCHAR, categorie_code VARCHAR,
    montant_meur DOUBLE NOT NULL, source VARCHAR NOT NULL
);

-- Recettes. categorie : 'fiscale', 'non_fiscale', 'prelevement' (PSR UE / collectivités),
-- 'fonds_concours', 'autre'. Les prélèvements sont stockés en positif.
-- sens : 'brut' ou 'net' (remboursements et dégrèvements déduits) quand la source le distingue.
CREATE TABLE IF NOT EXISTS recette (
    exercice INTEGER NOT NULL, mois INTEGER, periode VARCHAR NOT NULL, nature VARCHAR NOT NULL,
    categorie VARCHAR NOT NULL, sens VARCHAR,
    poste_code VARCHAR, poste_lib VARCHAR,
    montant_meur DOUBLE NOT NULL, source VARCHAR NOT NULL
);

-- Agrégats de l'État (périmètre DGFiP) : solde, dette financière, charge de la dette…
CREATE TABLE IF NOT EXISTS agregat_etat (
    exercice INTEGER NOT NULL, mois INTEGER, periode VARCHAR NOT NULL,
    indicateur VARCHAR NOT NULL, montant_meur DOUBLE NOT NULL, source VARCHAR NOT NULL
);

-- Agrégats macro : PIB nominal ; dette / déficit des APU au sens de Maastricht.
-- perimetre : 'economie' (PIB) ou 'APU' (Maastricht). Jamais mélangé avec agregat_etat.
CREATE TABLE IF NOT EXISTS macro (
    annee INTEGER NOT NULL, indicateur VARCHAR NOT NULL, perimetre VARCHAR NOT NULL,
    valeur_meur DOUBLE NOT NULL, source VARCHAR NOT NULL
);

-- Montants de référence publiés, pour contrôler la cohérence des tables ci-dessus.
CREATE TABLE IF NOT EXISTS controle (
    exercice INTEGER NOT NULL, cle VARCHAR NOT NULL, reference DOUBLE NOT NULL, source VARCHAR NOT NULL
);

-- Séries brutes (INSEE…) conservées telles quelles (valeur × 10^puissance = unité).
CREATE TABLE IF NOT EXISTS serie (
    source VARCHAR, serie VARCHAR, titre VARCHAR, periode VARCHAR, valeur DOUBLE, unite VARCHAR, puissance DOUBLE
);

-- Correspondance de nomenclature (voir nomenclature.py).
CREATE TABLE IF NOT EXISTS nomenclature_programme (
    exercice INTEGER NOT NULL, programme_code VARCHAR NOT NULL, programme_lib VARCHAR,
    mission_lib VARCHAR, programme_canon VARCHAR, mission_canon VARCHAR, methode VARCHAR NOT NULL
);
"""


def connect(path: Path | str = DB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con
