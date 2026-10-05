"""Tests des programmes : validation des sources, résolution des cibles, API.
Programmes et montants FICTIFS (porteur « Test »), pour exercer le code."""

import json

import pytest
from fastapi.testclient import TestClient

from budget_etat import ingest, programmes as prog
from budget_etat.server import create_app
from test_pipeline import fixture_parser

SRC = {"url": "https://exemple.invalid/programme", "citation": "« citation de test »"}


def mesure(**kw):
    m = {"libelle": "test", "cote": "depense", "cible": "M:Mission A", "mode": "pct", "valeur": -10,
         "debut": 2002, "montee_en_charge": 2, "chiffrage": "tiers", "source": SRC}
    m.update(kw)
    return m


def programme(*mesures):
    return {"nom": "Programme test", "porteur": "Test", "mesures": list(mesures)}


def test_validation_requires_source_unless_user_hypothesis():
    with pytest.raises(prog.ProgrammeInvalide, match="sans source"):
        prog.valider(programme(mesure(source={})), "x")
    prog.valider(programme(mesure(source=None, chiffrage="utilisateur")), "x")  # admis, signalé


def test_validation_never_guesses_values():
    with pytest.raises(prog.ProgrammeInvalide, match="valeur"):
        prog.valider(programme(mesure(valeur=None)), "x")
    with pytest.raises(prog.ProgrammeInvalide, match="cote"):
        prog.valider(programme(mesure(cote="dépenses")), "x")


BASE = {
    "spending": [
        {"key": "P:101", "group": "M:Mission A", "group_label": "Mission A"},
        {"key": "P:102", "group": "M:Écologie, développement", "group_label": "Écologie, développement"},
        {"key": "P:103", "group": "M:Écologie, mobilité", "group_label": "Écologie, mobilité"},
    ],
    "revenue": [{"key": "R:TVA", "group": "C:fiscale", "group_label": "fiscale"}],
}


@pytest.mark.parametrize("cible,cote,target,erreur", [
    ("P:101", "depense", "P:101", None),
    ("M:mission a", "depense", "M:Mission A", None),  # casse / accents ignorés
    ("M:ecologie developpement", "depense", "M:Écologie, développement", None),
    ("M:Écologie", "depense", None, "ambigu"),
    ("M:Inconnue", "depense", None, "introuvable"),
    ("R:TVA", "recette", "R:TVA", None),
    ("C:fiscale", "recette", "C:fiscale", None),
    ("*", "recette", "*", None),
    ("R:TVA", "depense", None, "introuvable"),  # mauvais côté
])
def test_resolution(cible, cote, target, erreur):
    (r,) = prog.resoudre(prog.valider(programme(mesure(cible=cible, cote=cote)), "x"), BASE)
    assert r["target"] == target
    assert (erreur or "") in (r["erreur"] or "") and bool(erreur) == bool(r["erreur"])


def test_api_programmes(tmp_path):
    raw = tmp_path / "raw"
    (raw / "fx").mkdir(parents=True)
    (raw / "fx" / "a.csv").write_text("x\n1\n")
    dbp = tmp_path / "b.duckdb"
    ingest.run(raw, dbp, [ingest.Parser("fx", "fx/*.csv", fixture_parser)])
    pdir = tmp_path / "programmes"
    pdir.mkdir()
    (pdir / "test.json").write_text(json.dumps(programme(mesure(), mesure(cible="M:Absente"))), encoding="utf-8")
    (pdir / "casse.json").write_text(json.dumps(programme(mesure(source={}))), encoding="utf-8")
    (pdir / "_modele.json").write_text("{}")
    c = TestClient(create_app(dbp, tmp_path / "sc", pdir))
    lst = {p["id"]: p for p in c.get("/api/programmes").json()}
    assert set(lst) == {"test", "casse"} and lst["casse"]["erreur"] and lst["test"]["nb_mesures"] == 2
    r = c.get("/api/programmes/test").json()
    assert r["exercice_base"] == 2001
    ok, ko = r["mesures"]
    assert ok["target"] == "M:Mission A" and ok["side"] == "spending" and ok["erreur"] is None
    assert ko["target"] is None and "introuvable" in ko["erreur"]
    assert c.get("/api/programmes/casse").status_code == 422
    assert c.get("/api/programmes/absent").status_code == 404
