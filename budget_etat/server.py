"""Serveur local : API JSON + front statique (web/)."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from budget_etat import db, programmes as prog, queries
from budget_etat.fetch import ROOT
from budget_etat.projection import Assumptions, BaseYear, Line, Measure, compare, project, solve_effort

WEB = ROOT / "web"
SCENARIOS = ROOT / "data" / "scenarios"


def create_app(db_path: Path = db.DB_PATH, scenarios_dir: Path = SCENARIOS,
               programmes_dir: Path = prog.PROGRAMMES_DIR) -> FastAPI:
    app = FastAPI(title="Budget de l'État")

    @contextmanager
    def con():
        if not Path(db_path).exists():
            # Base vide (schéma seul) : le front affiche « pas de données ».
            c = db.connect(":memory:")
        else:
            c = db.connect(db_path, read_only=True)
        try:
            yield c
        finally:
            c.close()

    @app.get("/api/status")
    def status():
        with con() as c:
            return {**queries.status(c), "db": str(db_path), "db_exists": Path(db_path).exists()}

    @app.get("/api/series")
    def series():
        with con() as c:
            return queries.series(c)

    @app.get("/api/sankey/{exercice}")
    def sankey(exercice: int):
        with con() as c:
            return queries.sankey(c, exercice)

    @app.get("/api/drill/{exercice}")
    def drill(exercice: int, mission: str | None = None, programme: str | None = None):
        with con() as c:
            return queries.drill(c, exercice, mission, programme)

    @app.get("/api/sim/base")
    def sim_base(exercice: int | None = None):
        with con() as c:
            return queries.sim_base(c, exercice)

    @app.post("/api/sim/run")
    def sim_run(req: SimRequest):
        try:
            base = req.base.to_model()
            a = Assumptions(**req.assumptions.model_dump())
            measures = [Measure(**m.model_dump()) for m in req.measures]
            ref = project(base, a)
            sc = project(base, a, measures)
        except (KeyError, ValueError, ZeroDivisionError) as e:
            raise HTTPException(422, str(e)) from e
        return {"perimetre": queries.PERIMETRE_ETAT, "reference": [_row(r) for r in ref],
                "scenario": [_row(r) for r in sc], "diff": compare(ref, sc)}

    @app.post("/api/sim/solve")
    def sim_solve(req: SolveRequest):
        """Effort annuel minimal (réparti dépenses / recettes) pour atteindre un objectif de dette."""
        try:
            base = req.base.to_model()
            a = Assumptions(**req.assumptions.model_dump())
            r = solve_effort(base, a, req.objectif, annee=req.annee, cible=req.cible, part_depenses=req.part_depenses,
                             debut=req.debut, montee=req.montee)
            ref = project(base, a)
        except (KeyError, ValueError, ZeroDivisionError) as e:
            raise HTTPException(422, str(e)) from e
        return {"effort_meur": r.effort_meur, "atteignable": r.atteignable, "valeur_atteinte": r.valeur_atteinte,
                "measures": [m.__dict__ for m in r.measures], "perimetre": queries.PERIMETRE_ETAT,
                "reference": [_row(x) for x in ref], "scenario": [_row(x) for x in r.results],
                "diff": compare(ref, r.results)}

    @app.get("/api/programmes")
    def list_programmes():
        return prog.lister(programmes_dir)

    @app.get("/api/programmes/{ident}")
    def get_programme(ident: str, exercice: int | None = None):
        try:
            p = prog.charger(ident, programmes_dir)
        except FileNotFoundError:
            raise HTTPException(404, "programme inconnu")
        except (prog.ProgrammeInvalide, json.JSONDecodeError) as e:
            raise HTTPException(422, str(e))
        with con() as c:
            base = queries.sim_base(c, exercice)
        return {"programme": p, "mesures": prog.resoudre(p, base), "exercice_base": base["exercice"]}

    @app.get("/api/scenarios")
    def list_scenarios():
        scenarios_dir.mkdir(parents=True, exist_ok=True)
        return sorted(p.stem for p in scenarios_dir.glob("*.json"))

    @app.get("/api/scenarios/{name}")
    def get_scenario(name: str):
        p = _scenario_path(scenarios_dir, name)
        if not p.exists():
            raise HTTPException(404, "scénario inconnu")
        return json.loads(p.read_text(encoding="utf-8"))

    @app.put("/api/scenarios/{name}")
    def put_scenario(name: str, body: dict):
        p = _scenario_path(scenarios_dir, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "name": p.stem}

    @app.delete("/api/scenarios/{name}")
    def delete_scenario(name: str):
        _scenario_path(scenarios_dir, name).unlink(missing_ok=True)
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/", StaticFiles(directory=WEB), name="web")
    return app


def _scenario_path(d: Path, name: str) -> Path:
    if not re.fullmatch(r"[\w\- ]{1,80}", name):
        raise HTTPException(400, "nom de scénario invalide")
    return d / f"{name}.json"


def _row(r) -> dict:
    return {"year": r.year, "gdp": r.gdp, "spending": r.spending, "interest": r.interest,
            "revenue": r.revenue, "deficit": r.deficit, "debt": r.debt, "apparent_rate": r.apparent_rate,
            "debt_to_gdp": r.debt_to_gdp, "deficit_to_gdp": r.deficit_to_gdp,
            "spending_by_line": r.spending_by_line, "revenue_by_line": r.revenue_by_line}


# --- modèles de requête -----------------------------------------------------
class LineIn(BaseModel):
    key: str
    amount: float
    group: str | None = None
    trend: float | None = None


class BaseIn(BaseModel):
    year: int
    gdp: float = Field(gt=0)
    debt: float
    interest: float
    spending: list[LineIn]
    revenue: list[LineIn]

    def to_model(self) -> BaseYear:
        return BaseYear(self.year, self.gdp, self.debt, self.interest,
                        [Line(**l.model_dump()) for l in self.spending],
                        [Line(**l.model_dump()) for l in self.revenue])


class AssumptionsIn(BaseModel):
    horizon: int = Field(10, ge=1, le=30)
    gdp_growth: float | list[float] = 0.03
    market_rate: float | list[float] = 0.03
    refinancing_share: float = Field(0.12, ge=0, le=1)
    spending_trend: float | list[float] = 0.02
    revenue_elasticity: float = 1.0
    multiplier_enabled: bool = False
    spending_multiplier: float = 0.0
    revenue_multiplier: float = 0.0
    other_debt_flows: float | list[float] = 0.0


class MeasureIn(BaseModel):
    side: str = Field(pattern="^(spending|revenue)$")
    target: str
    mode: str = Field(pattern="^(pct|meur)$")
    value: float
    start_year: int
    ramp_years: int = Field(1, ge=1)


class SimRequest(BaseModel):
    base: BaseIn
    assumptions: AssumptionsIn = AssumptionsIn()
    measures: list[MeasureIn] = []


class SolveRequest(BaseModel):
    base: BaseIn
    assumptions: AssumptionsIn = AssumptionsIn()
    objectif: str = Field(pattern="^(ratio_baisse|ratio_base|ratio_cible|solde_equilibre)$")
    annee: int
    cible: float | None = Field(None, gt=0, lt=5)  # fraction du PIB (0,85 = 85 %)
    part_depenses: float = Field(0.5, ge=0, le=1)
    debut: int
    montee: int = Field(1, ge=1)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    print(f"→ http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port)
