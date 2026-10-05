"""Tests des parseurs sur des fichiers qui reproduisent les FORMATS observés dans
data/INSPECTION.md (en-têtes, séparateurs, virgules décimales, encodages).
Les montants sont abstraits : ce ne sont pas des données budgétaires.
"""

import io
import zipfile

import pytest

from budget_etat import ingest, inspect_raw, queries, sources
from budget_etat.db import connect
from budget_etat.fetch import select_datasets


def write(path, text, enc="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(enc))
    return path


# --- utilitaires -----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1 234,56", 1234.56), ("-8836054,82", -8836054.82), ("1234.0", 1234.0), ("13 498", 13498.0),
    ("1.234,5", 1234.5), ("", None), (None, None), (42, 42.0),
])
def test_to_float(raw, expected):
    assert sources.to_float(raw) == expected


def test_split_programme_and_perimetre():
    assert sources.split_programme("Action de la France en Europe et dans le monde - 105") == \
        ("105", "Action de la France en Europe et dans le monde")
    assert sources.split_programme("Sans code") == (None, "Sans code")
    assert [sources.perimetre_programme(c) for c in ("105", "552", "613", "741", "862", "951", None)] == \
        ["BG", "BG", "BA", "CAS", "CCF", "CC", "inconnu"]


def test_year_from_path(tmp_path):
    assert sources.year_from_path(tmp_path / "economie/plrg-2024/attachments/annexe1_etat_cp_2024_csv") == 2024
    assert sources.year_from_path(tmp_path / "economie/execution-2011-du-budget-general-en-cp/export.csv") == 2011


def test_decode_cp1252():
    assert sources.decode_bytes("Prélèvement".encode("cp1252")) == ("Prélèvement", "cp1252")


def test_select_datasets():
    cat = [{"id": i, "title": t} for i, t in [
        ("plrg-2023", ""), ("plr-2019-donnees-execution", ""), ("execution-2016-du-budget-general-en-cp", ""),
        ("execution-2016-du-budget-general-en-ae", ""), ("plr-2015-projet-de-loi-de-reglement-pour-2015-donnees-du-volet-performance-prese", ""),
        ("plf-2026-budget-vert", "PLF 2026 - Budget vert"), ("x", "PLF 2019 - Recettes fiscales nettes"),
        ("y", "Annuaire statistique")]]
    assert select_datasets(cat) == ["plrg-2023", "plr-2019-donnees-execution",
                                    "execution-2016-du-budget-general-en-cp", "x"]


# --- formats d'exécution « par titre » ----------------------------------------------

EXEC_2011 = """mission;programme;libelle_programme;action;libelle_action;t1;t2;t3;t4;t5;t6;t7;total_general
Mission A;101;Programme A1;01;Action 1;;2000000;1000000;;;;;3000000
Mission A;101;Programme A1;02;Action 2;;500000;;;;;;500000
Engagements financiers de l'État;117;Charge de la dette et trésorerie de l'État;01;Dette;;;;4000000;;;;4000000
Remboursements et dégrèvements;200;Remboursements et dégrèvements d'impôts d'État;01;RD;;;;;;9000000;;9000000
Pensions;741;Pensions civiles;01;CAS;;7000000;;;;;;7000000
"""
EXEC_2010 = """ministere;libelle;pgm;libelle_pgm;action;libelle_action;t1;t2;t3;t4;t5;t6;t7;total_general
07;Ministère X;101;Programme A1;01;Action 1;;1000000;;;;;;1000000
"""
EXEC_2012 = """mission;programme;libelle_programme;action;libelle_action;t1;t2;t3;t4;t5;t6;t7
Mission A;101;Programme A1;01;Action 1;;3000000;;;;;
"""


@pytest.fixture
def raw(tmp_path):
    r = tmp_path / "raw"
    e = r / "economie"
    write(e / "execution-2011-du-budget-general-en-cp/export.csv", EXEC_2011)
    write(e / "execution-2010-du-budget-general-en-cp/export.csv", EXEC_2010)
    write(e / "execution-2012-du-budget-de-letat-en-cp-suivant-la-nomenclature-mission-programm/export.csv", EXEC_2012)
    write(e / "execution-2012-du-budget-de-letat-en-cp-suivant-la-nomenclature-ministere-progra/export.csv", EXEC_2012)
    write(e / "execution-2012-du-budget-de-letat-en-ae-suivant-la-nomenclature-mission-programm/export.csv", EXEC_2012)
    a = e / "plrg-2024" / "attachments"
    write(a / "annexe1_etat_titre_cat_2024_csv",
          "Mission;Programme;Titre;Categorie;Depenses\n"
          "Mission A;Programme A1 - 101;Titre 2;21;1500000,5\n"
          "Mission A;Programme A1 - 101;Titre 3;31;500000\n"
          "Engagements financiers de l'État;Charge de la dette et trésorerie de l'État - 117;Titre 4;41;4000000\n")
    write(a / "annexe1_etat_cp_2024_csv",
          "Mission;Programme;Titre;LFI;Depenses_constatees\n"
          "Mission A;Programme A1 - 101;Titre 2 - Dépenses de personnel;0;1500000,5\n"
          "Mission A;Programme A1 - 101;Autres titres - Autres dépenses;0;500000\n"
          "Engagements financiers de l'État;Charge de la dette et trésorerie de l'État - 117;Autres titres - Autres dépenses;0;4100000\n")
    write(a / "annexe1_etat_recettes_csv",
          "Niveau hiérarchique de la ligne;Catégorie;Section;Ligne de prévision;Ligne d'exécution;LFI;LFR/LFG;"
          "Total des prévisions;Total des recouvrements;Total des recettes**\n"
          "0;Recettes fiscales;11 - Impôt X;1101 - Impôt X;;0;0;0;0;0\n"
          "1;Recettes fiscales;11 - Impôt X;1101 - Impôt X;110101 - détail;0;0;0;7000000;0\n"
          "2;Recettes fiscales;11 - Impôt X (total);11 - Impôt X (total);;0;0;0;0;6000000\n"
          "2;Recettes fiscales;16 - Impôt Y (total);16 - Impôt Y (total);;0;0;0;0;2000000,5\n"
          "3;Recettes fiscales (total);Recettes fiscales (total);Recettes fiscales (total);;0;0;0;0;8000000,5\n"
          "2;Prélèvements sur les recettes de l'État;32 - Prélèvement UE;3201 - PSR UE;;0;0;0;0;-1000000\n"
          "3;Prélèvements sur les recettes de l'État (total);Prélèvements sur les recettes de l'État (total);x;;0;0;0;0;-1000000\n")
    write(a / "bacea_bilan_2024_csv", "a;b\n1;2\n")
    return r


@pytest.fixture
def db(raw, tmp_path):
    p = tmp_path / "b.duckdb"
    assert ingest.run(raw, p) == 0
    return connect(p, read_only=True)


def test_exec_aggregates_actions_and_keeps_bg_only(db):
    rows = db.execute("SELECT programme_code, titre_code, montant_meur FROM depense WHERE exercice = 2011 "
                      "ORDER BY 1, 2").fetchall()
    assert rows == [("101", "2", 2.5), ("101", "3", 1.0), ("117", "4", 4.0), ("200", "6", 9.0)]  # 741 exclu


def test_exec_2010_mission_deduced_from_nomenclature(db):
    assert db.execute("SELECT mission_lib, mission_code FROM depense WHERE exercice = 2010").fetchall() == \
        [("Mission A", "déduite")]


def test_exec_ministere_and_ae_files_skipped(db):
    assert db.execute("SELECT sum(montant_meur) FROM depense WHERE exercice = 2012").fetchone()[0] == 3.0


def test_plrg_titre_cat_and_controls(db, tmp_path):
    assert db.execute("SELECT sum(montant_meur) FROM depense WHERE exercice = 2024").fetchone()[0] == \
        pytest.approx(6.0000005)
    assert db.execute("SELECT categorie_code FROM depense WHERE exercice = 2024 AND titre_code = '2'").fetchone() == ("21",)
    report = (tmp_path / "VALIDATION.md").read_text()
    assert "programme 117" in report and "-0.10" in report  # écart signalé
    assert "programme 101" not in report  # contrôle exact, non listé


def test_plrg_recettes_section_totals(db, tmp_path):
    rows = dict(db.execute("SELECT poste_lib, montant_meur FROM recette WHERE exercice = 2024").fetchall())
    assert rows == {"Impôt X": 6.0, "Impôt Y": 2.0000005, "Prélèvement UE": 1.0}  # prélèvement stocké positif
    s = queries.series(db)["etat"]
    i = s["years"].index(2024)
    assert s["recettes_nettes"][i] == pytest.approx(7.0000005)
    assert s["depenses"][i] == pytest.approx(6.0000005)  # 200/201 exclus, aucun en 2024 ici
    assert "recettes fiscale" not in (tmp_path / "VALIDATION.md").read_text()  # 8,0000005 = 6 + 2,0000005


def test_net_view_excludes_remboursements(db):
    s = queries.series(db)["etat"]
    assert s["depenses"][s["years"].index(2011)] == pytest.approx(7.5)  # 9 M€ du programme 200 exclus
    assert s["charge_dette"][s["years"].index(2011)] == pytest.approx(4.0)


def test_unused_attachment_is_skipped_not_error(raw, tmp_path, capsys):
    ingest.run(raw, tmp_path / "c.duckdb")
    out = capsys.readouterr().out
    assert "0 erreur(s)" in out and "ignoré(s)" in out


def test_unknown_format_is_reported(tmp_path, capsys):
    raw = tmp_path / "raw"
    write(raw / "economie/plrg-2030/attachments/annexe1_etat_titre_cat_2030_csv", "foo;bar\n1;2\n")
    assert ingest.run(raw, tmp_path / "d.duckdb") == 0
    assert "en-têtes ['foo', 'bar']" in capsys.readouterr().out


# --- PLF ---------------------------------------------------------------------------

def test_plf_recettes_and_depenses(tmp_path):
    rec = write(tmp_path / "plf-2024-recettes-du-budget-general/export.csv",
                "annee;type_de_recettes;code_ligne_recettes;libelle;montant_recettes_plf;montant_recettes_lfi\n"
                "2024.0;Recettes fiscales;1101.0;Impôt X;1000000.0;2000000.0\n")
    out = list(sources.parse_plf_recettes(rec))
    assert [(r["nature"], r["poste_code"], r["montant_meur"]) for _, r in out] == [("plf", "1101", 1.0), ("lfi", "1101", 2.0)]
    dep = write(tmp_path / "plf25-depenses-2025-selon-destination/export.csv",
                "exercice;loi;typebudget;ministere;libelle_ministere;mission;libelle_mission;programme;libelle_programme;"
                "action;libelle_action;sous_action;libelle_sous_action;categorie;titre;autorisation_engagement;credit_de_paiement\n"
                "2025;PLF;BG;1;M;AA;Mission A;101;Programme A1;101-01;a;;;21;2;0;1000000.0\n"
                "2025;PLF;BG;1;M;AA;Mission A;101;Programme A1;101-02;b;;;21;2;0;500000.0\n"
                "2025;PLF;CAS;1;M;ZZ;CAS;741;P;741-01;c;;;21;2;0;9000000.0\n")
    out = [r for _, r in sources.parse_plf_depenses(dep)]
    assert len(out) == 1 and out[0]["montant_meur"] == 1.5 and out[0]["nature"] == "plf"


# --- INSEE SDMX ------------------------------------------------------------------------

SDMX = """<?xml version="1.0" encoding="UTF-8"?>
<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message">
<message:DataSet>
<Series IDBANK="000000001" TITLE_FR="Encours de la dette négociable de l'État en euros" UNIT_MULT="9" UNIT_MEASURE="EUROS">
  <Obs TIME_PERIOD="2023-12" OBS_VALUE="2,5"/><Obs TIME_PERIOD="2024-12" OBS_VALUE="3.0"/>
</Series>
<Series IDBANK="001711532" TITLE_FR="Encours de la dette négociable de l'État à court terme (maturité d'un an et moins) en euros" UNIT_MULT="9">
  <Obs TIME_PERIOD="2024-12" OBS_VALUE="1.0"/>
</Series>
</message:DataSet></message:StructureSpecificData>"""


def test_insee_dette(tmp_path):
    p = write(tmp_path / "insee/dette_negociable.xml", SDMX)
    rows = list(sources.parse_insee(p))
    dette = [r for t, r in rows if t == "agregat_etat"]
    assert [(r["exercice"], r["mois"], r["montant_meur"]) for r in dette] == [(2023, 12, 2500.0), (2024, 12, 3000.0)]
    assert sum(1 for t, _ in rows if t == "serie") == 3


def test_insee_without_unit_mult_is_refused(tmp_path):
    p = write(tmp_path / "x.xml", SDMX.replace(' UNIT_MULT="9" UNIT_MEASURE="EUROS"', ""))
    with pytest.raises(sources.UnknownFormat, match="UNIT_MULT"):
        list(sources.parse_insee(p))


# --- inspection -------------------------------------------------------------------------

def test_inspect_cp1252_zip_and_catalog(tmp_path):
    raw = tmp_path / "raw"
    write(raw / "a/f_csv", "Libellé;Montant\nPrélèvement;1 234\n", enc="cp1252")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notice.txt", "Total des recettes** : nettes des remboursements")
        z.writestr("data.csv", "x;y\n1;2\n")
    (raw / "a/notices_zip").write_bytes(buf.getvalue())
    write(raw / "economie/_catalog_all.json", '[{"id": "plrg-2023", "title": "PLRG 2023"}]')
    write(raw / "insee/dette_negociable.xml", SDMX)
    rep = tmp_path / "I.md"
    assert inspect_raw.run(raw, rep) == 0
    t = rep.read_text()
    assert "encodage : cp1252" in t and "Prélèvement" in t
    assert "nettes des remboursements" in t and "(zip) `data.csv`" in t
    assert "`plrg-2023` | PLRG 2023" in t
    assert "000000001" in t and "2023-12 → 2024-12" in t
