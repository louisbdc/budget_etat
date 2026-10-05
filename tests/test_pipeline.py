"""Tests de la chaîne ingestion -> requêtes -> API.

FIXTURES ABSTRAITES : les libellés (« Mission A »…) et montants sont inventés
pour exercer le code ; ce ne sont pas des données budgétaires.
"""

import json

import pytest
from fastapi.testclient import TestClient

from budget_etat import ingest, inspect_raw, nomenclature, queries
from budget_etat.db import connect
from budget_etat.jsonstat import decode
from budget_etat.server import create_app


# --- JSON-stat ----------------------------------------------------------------

def jsonstat_fixture():
    return {
        "id": ["unit", "na_item", "geo", "time"],
        "size": [1, 2, 1, 2],
        "dimension": {
            "unit": {"category": {"index": {"MIO_EUR": 0}}},
            "na_item": {"category": {"index": {"GD": 0, "B9": 1}}},
            "geo": {"category": {"index": ["FR"]}},
            "time": {"category": {"index": {"2001": 0, "2000": 1}}},  # ordre via index, pas via clés
        },
        "value": {"0": 10.0, "1": 9.0, "3": -1.5},  # (B9, 2001) manquant
    }


def test_jsonstat_decode_row_major_and_sparse():
    rows = decode(jsonstat_fixture())
    assert {(r["na_item"], r["time"]): r["value"] for r in rows} == {
        ("GD", "2001"): 10.0, ("GD", "2000"): 9.0, ("B9", "2000"): -1.5}


def test_jsonstat_dense_values():
    ds = jsonstat_fixture()
    ds["value"] = [1, 2, None, 4]
    assert len(decode(ds)) == 3


# --- nomenclature ---------------------------------------------------------------

def P(ex, code, lib, mission="Mission A"):
    return nomenclature.Programme(ex, code, lib, mission)


def test_reconcile_rules():
    progs = [
        P(2002, "101", "Accès au droit et à la justice"),
        P(2001, "101", "Accès au droit et justice"),  # libellé proche
        P(2000, "101", "Météorologie nationale"),  # même code, autre objet
        P(2000, "999", "Programme disparu"),
        P(2000, "888", "Programme renommé"),
    ]
    manual = {(None, "888"): ("101", None)}
    m = {(x.exercice, x.programme_code): x for x in nomenclature.reconcile(progs, manual)}
    assert m[(2002, "101")].methode == "code+libelle"
    assert m[(2001, "101")].methode == "code+libelle"
    assert m[(2000, "101")].methode == "code_seul"
    assert m[(2000, "999")].methode == "non_rapproche"
    assert m[(2000, "888")].methode == "manuel"
    assert m[(2000, "888")].mission_canon == "Mission A"


def test_similarity_ignores_accents_and_stopwords():
    assert nomenclature.similarity("Écologie et développement", "ecologie developpement") == 1.0


# --- ingestion + requêtes ---------------------------------------------------------

def fixture_parser(path):
    for ex in (2000, 2001):
        k = 1 if ex == 2000 else 1.1
        for mission, code, lib, titre, v in [
            ("Mission A", "101", "Programme A1", "2", 40), ("Mission A", "101", "Programme A1", "3", 20),
            ("Mission B", "102", "Programme B1", "6", 30), ("Mission C", "117", "Charge de la dette", "4", 10),
        ]:
            yield "depense", dict(exercice=ex, mois=12, periode="cumul_mensuel", nature="execution",
                                  mission_code=None, mission_lib=mission, programme_code=code, programme_lib=lib,
                                  titre_code=titre, titre_lib=f"Titre {titre}", montant_meur=v * k, source="fixture")
        for cat, poste, v in [("fiscale", "Impôt X", 70), ("fiscale", "Impôt Y", 20),
                              ("non_fiscale", "Divers", 5), ("prelevement", "Prélèvement Union européenne", 5)]:
            yield "recette", dict(exercice=ex, mois=12, periode="cumul_mensuel", nature="execution", categorie=cat,
                                  sens="net", poste_code=None, poste_lib=poste, montant_meur=v * k, source="fixture")
        yield "agregat_etat", dict(exercice=ex, mois=12, periode="cumul_mensuel", indicateur="dette_etat",
                                   montant_meur=500 * k, source="fixture")
        yield "macro", dict(annee=ex, indicateur="pib_nominal", perimetre="economie",
                            valeur_meur=1000 * k, source="fixture")


def failing_parser(path):
    raise RuntimeError("boom")
    yield


@pytest.fixture
def built_db(tmp_path):
    raw = tmp_path / "raw"
    (raw / "fx").mkdir(parents=True)
    (raw / "fx" / "a.csv").write_text("x\n1\n")
    dbp = tmp_path / "b.duckdb"
    parsers = [ingest.Parser("fx", "fx/*.csv", fixture_parser)]
    assert ingest.run(raw, dbp, parsers) == 0
    return raw, dbp, parsers


def test_ingest_is_idempotent(built_db):
    raw, dbp, parsers = built_db
    ingest.run(raw, dbp, parsers)
    con = connect(dbp, read_only=True)
    assert con.execute("SELECT count(*) FROM depense").fetchone()[0] == 8
    assert con.execute("SELECT count(*) FROM source_file").fetchone()[0] == 1


def test_failed_ingest_keeps_previous_db(built_db):
    raw, dbp, parsers = built_db
    assert ingest.run(raw, dbp, [ingest.Parser("fx", "fx/*.csv", failing_parser)]) == 1
    # La base a été reconstruite sans les lignes du parseur en échec, mais reste valide.
    connect(dbp, read_only=True).execute("SELECT count(*) FROM depense").fetchone()


def test_eurostat_parser(tmp_path):
    ds = jsonstat_fixture()
    ds["id"].insert(1, "sector")
    ds["size"].insert(1, 1)
    ds["dimension"]["sector"] = {"category": {"index": {"S13": 0}}}
    p = tmp_path / "gov_10dd_edpt1.json"
    p.write_text(json.dumps(ds))
    rows = list(ingest.parse_eurostat(p))
    assert ("macro", {"annee": 2001, "indicateur": "dette_maastricht", "perimetre": "APU",
                      "valeur_meur": 10.0, "source": "Eurostat gov_10dd_edpt1"}) in rows


def test_eurostat_parser_reports_missing_codes(tmp_path):
    p = tmp_path / "gov_10dd_edpt1.json"
    p.write_text(json.dumps(jsonstat_fixture()))  # pas de dimension sector
    with pytest.raises(ValueError, match="Codes présents"):
        list(ingest.parse_eurostat(p))


def test_series_and_perimeters(built_db):
    _, dbp, _ = built_db
    s = queries.series(connect(dbp, read_only=True))
    e = s["etat"]
    assert e["years"] == [2000, 2001]
    assert e["depenses"][0] == pytest.approx(100)
    assert e["recettes_nettes"][0] == pytest.approx(90)  # 95 - 5 de prélèvement
    assert e["solde"][0] == pytest.approx(-10)
    assert e["charge_dette"][0] == pytest.approx(10)
    assert e["dette_etat"][1] == pytest.approx(550)
    assert "dette_maastricht" not in e  # périmètres séparés


def test_drill_down_levels(built_db):
    _, dbp, _ = built_db
    c = connect(dbp, read_only=True)
    top = queries.drill(c, 2000)
    assert [i["label"] for i in top["items"]][0] == "Mission A"
    assert top["items"][0]["pct_total"] == pytest.approx(60)
    assert top["items"][0]["pct_pib"] == pytest.approx(6)
    prog = queries.drill(c, 2000, mission="Mission A")
    assert prog["items"][0]["code"] == "101"
    titres = queries.drill(c, 2000, programme="101")
    assert {i["code"]: i["rigidite"] is not None for i in titres["items"]} == {"2": True, "3": False}
    dette = queries.drill(c, 2000, mission="Mission C")
    assert dette["items"][0]["rigidite"]["niveau"] == "fixe"


def test_sankey_balances(built_db):
    _, dbp, _ = built_db
    sk = queries.sankey(connect(dbp, read_only=True), 2000)
    inflow = sum(l["value"] for l in sk["links"] if l["target"] == "Budget général")
    outflow = sum(l["value"] for l in sk["links"] if l["source"] == "Budget général")
    assert inflow == pytest.approx(outflow)  # recettes + déficit = missions + prélèvements


def test_sim_base_excludes_interest_programme(built_db):
    _, dbp, _ = built_db
    b = queries.sim_base(connect(dbp, read_only=True))
    assert b["exercice"] == 2001
    assert {l["key"] for l in b["spending"]} == {"P:101", "P:102"}
    assert b["interest"] == pytest.approx(11)
    assert b["missing"] == []
    assert any(l["amount"] < 0 for l in b["revenue"])  # prélèvement en négatif


# --- API ------------------------------------------------------------------------

def test_api_end_to_end(built_db, tmp_path):
    _, dbp, _ = built_db
    client = TestClient(create_app(dbp, tmp_path / "sc"))
    assert client.get("/api/status").json()["years"] == [2000, 2001]
    base = client.get("/api/sim/base").json()
    body = {
        "base": {"year": base["exercice"], "gdp": base["gdp"], "debt": base["debt"], "interest": base["interest"],
                 "spending": [{"key": l["key"], "amount": l["amount"], "group": l["group"]} for l in base["spending"]],
                 "revenue": [{"key": l["key"], "amount": l["amount"], "group": l["group"]} for l in base["revenue"]]},
        "assumptions": {"horizon": 5},
        "measures": [{"side": "spending", "target": "M:Mission A", "mode": "pct", "value": -10, "start_year": 2002,
                      "ramp_years": 2}],
    }
    r = client.post("/api/sim/run", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["scenario"]) == 6
    assert out["diff"][1]["d_deficit"] < 0
    assert client.put("/api/scenarios/test 1", json=body).json()["ok"]
    assert client.get("/api/scenarios").json() == ["test 1"]
    assert client.get("/api/scenarios/test 1").json()["measures"][0]["value"] == -10
    assert client.put("/api/scenarios/..%2Fx", json={}).status_code in (400, 404, 405)
    assert client.put("/api/scenarios/a.b", json={}).status_code == 400
    bad = dict(body, measures=[dict(body["measures"][0], target="inconnu")])
    assert client.post("/api/sim/run", json=bad).status_code == 422


def test_api_without_db(tmp_path):
    client = TestClient(create_app(tmp_path / "absent.duckdb", tmp_path / "sc"))
    st = client.get("/api/status").json()
    assert st["db_exists"] is False and st["years"] == []
    assert client.get("/api/sim/base").json()["missing"]
    assert client.get("/").status_code == 200


# --- inspection ------------------------------------------------------------------

def test_inspect_profiles_csv_and_json(tmp_path):
    raw = tmp_path / "raw"
    (raw / "s").mkdir(parents=True)
    (raw / "s" / "f.csv").write_text("Exercice;Mission;Montant\n2000;A;1,5\n2000;B;2\n", encoding="latin-1")
    (raw / "s" / "m.json").write_text('{"a": [1, 2], "b": {"c": "x"}}')
    report = tmp_path / "I.md"
    assert inspect_raw.run(raw, report) == 0
    txt = report.read_text()
    assert "`Mission`" in txt and "2 lignes" in txt and "[2 × int]" in txt


def test_api_solve(built_db, tmp_path):
    _, dbp, _ = built_db
    client = TestClient(create_app(dbp, tmp_path / "sc"))
    b = client.get("/api/sim/base").json()
    base = {"year": b["exercice"], "gdp": b["gdp"], "debt": b["debt"], "interest": b["interest"],
            "spending": [{"key": l["key"], "amount": l["amount"], "group": l["group"]} for l in b["spending"]],
            "revenue": [{"key": l["key"], "amount": l["amount"], "group": l["group"]} for l in b["revenue"]]}
    body = {"base": base, "assumptions": {"horizon": 6}, "objectif": "solde_equilibre", "annee": 2005,
            "part_depenses": 0.5, "debut": 2002, "montee": 2}
    r = client.post("/api/sim/solve", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["atteignable"] and out["effort_meur"] > 0 and out["valeur_atteinte"] <= 0
    assert {m["side"] for m in out["measures"]} == {"spending", "revenue"}
    assert all(m["target"] == "*" for m in out["measures"])
    # rejouer les mesures renvoyées donne le même résultat
    run = client.post("/api/sim/run", json={"base": base, "assumptions": {"horizon": 6}, "measures": out["measures"]}).json()
    assert run["scenario"][4]["deficit"] == pytest.approx(out["scenario"][4]["deficit"])
    assert client.post("/api/sim/solve", json={**body, "objectif": "ratio_cible"}).status_code == 422
    assert client.post("/api/sim/solve", json={**body, "objectif": "n_importe"}).status_code == 422
