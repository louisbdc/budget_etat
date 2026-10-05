"""Moteur de projection pluriannuelle du solde et de la dette de l'État.

Module pur : aucune I/O, aucune dépendance aux sources de données. Toutes les
valeurs de départ et les hypothèses sont passées en paramètre (en M€ courants),
ce qui permet de le tester isolément et de l'alimenter plus tard depuis la base.

Périmètre : ÉTAT uniquement (budget général, données DGFiP). La dette publique
au sens de Maastricht (APU : État + ASSO + APUL) n'est PAS calculée ici.

Conventions de signe :
- `deficit` > 0 signifie un besoin de financement (solde négatif).
- `solde` = recettes - dépenses totales (= -deficit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PERIMETRE = "État (budget général)"


@dataclass(frozen=True)
class Line:
    """Une ligne de dépense (hors charge de la dette) ou de recette."""

    key: str
    amount: float  # M€, année de base
    group: str | None = None  # ex. mission pour une ligne programme
    # Croissance tendancielle annuelle propre à la ligne (dépenses uniquement).
    # None => on utilise Assumptions.spending_trend.
    trend: float | None = None


@dataclass(frozen=True)
class Measure:
    """Variation appliquée à une ligne (ou à un groupe de lignes).

    - mode "pct" : `value` en % de la ligne de référence de l'année (ex. -5.0).
    - mode "meur" : `value` en M€ courants (ex. -1000.0).
    La mesure démarre en `start_year` et monte linéairement en charge sur
    `ramp_years` années (1 = plein effet dès la première année).
    """

    side: Literal["spending", "revenue"]
    target: str  # clé de ligne ou nom de groupe
    mode: Literal["pct", "meur"]
    value: float
    start_year: int
    ramp_years: int = 1

    def phase(self, year: int) -> float:
        if year < self.start_year:
            return 0.0
        ramp = max(1, self.ramp_years)
        return min(1.0, (year - self.start_year + 1) / ramp)


@dataclass(frozen=True)
class Assumptions:
    horizon: int = 10  # nombre d'années projetées après l'année de base
    gdp_growth: float | list[float] = 0.03  # croissance nominale du PIB
    market_rate: float | list[float] = 0.03  # taux d'émission des nouveaux titres
    # Part du stock de dette refinancée chaque année au taux de marché
    # (~ 1 / maturité moyenne résiduelle). 1.0 = tout refinancé immédiatement.
    refinancing_share: float = 0.12
    spending_trend: float | list[float] = 0.02  # croissance tendancielle des dépenses
    revenue_elasticity: float = 1.0  # élasticité des recettes au PIB nominal
    multiplier_enabled: bool = False
    spending_multiplier: float = 0.0  # ΔPIB = m · Δdépense
    revenue_multiplier: float = 0.0  # ΔPIB = -m · Δrecette
    # Flux de dette non expliqués par le solde (trésorerie, primes/décotes…), M€/an.
    other_debt_flows: float | list[float] = 0.0


@dataclass(frozen=True)
class BaseYear:
    year: int
    gdp: float  # PIB nominal, M€
    debt: float  # dette financière de l'État fin d'année, M€
    interest: float  # charge de la dette de l'année, M€
    spending: list[Line]  # dépenses hors charge de la dette
    revenue: list[Line]

    @property
    def apparent_rate(self) -> float:
        """Taux apparent implicite de l'année de base (intérêts / dette)."""
        return self.interest / self.debt if self.debt else 0.0


@dataclass
class YearResult:
    year: int
    gdp: float
    spending: float  # dépenses primaires (hors intérêts)
    interest: float
    revenue: float
    deficit: float
    debt: float
    apparent_rate: float
    spending_by_line: dict[str, float] = field(default_factory=dict)
    revenue_by_line: dict[str, float] = field(default_factory=dict)

    @property
    def total_spending(self) -> float:
        return self.spending + self.interest

    @property
    def solde(self) -> float:
        return -self.deficit

    @property
    def debt_to_gdp(self) -> float:
        return self.debt / self.gdp if self.gdp else float("nan")

    @property
    def deficit_to_gdp(self) -> float:
        return self.deficit / self.gdp if self.gdp else float("nan")


def _at(value: float | list[float], i: int) -> float:
    """Hypothèse scalaire ou chronique (index 0 = 1re année projetée)."""
    if isinstance(value, (int, float)):
        return float(value)
    return float(value[min(i, len(value) - 1)])


def roll_rate(rate: float, market_rate: float, refinancing_share: float) -> float:
    """Taux moyen du stock existant après refinancement d'une part de celui-ci.

    Une part `refinancing_share` du stock est réémise au taux de marché, le
    reste garde son taux historique : la dette ne se reprend pas d'un coup.
    """
    return (1 - refinancing_share) * rate + refinancing_share * market_rate


def blend_rate(rate: float, debt: float, borrowing: float, market_rate: float) -> float:
    """Taux moyen après ajout d'un emprunt net `borrowing` émis au taux de marché.

    Un désendettement (borrowing <= 0) réduit le stock sans changer son taux.
    """
    new_debt = debt + borrowing
    if borrowing <= 0 or new_debt <= 0:
        return rate
    return (debt * rate + borrowing * market_rate) / new_debt


def _measure_delta(m: Measure, year: int, baseline: dict[str, float], groups: dict[str, list[str]]) -> dict[str, float]:
    """Ventile l'effet d'une mesure (M€) sur les lignes visées."""
    phase = m.phase(year)
    if phase == 0.0:
        return {}
    # Cible : une ligne, un groupe (mission, catégorie de recettes) ou « * » (toutes
    # les lignes du côté visé, ex. « économies non ventilées »).
    keys = list(baseline) if m.target == "*" else [m.target] if m.target in baseline else groups.get(m.target, [])
    if not keys:
        raise KeyError(f"Cible de mesure inconnue : {m.target!r}")
    total = sum(baseline[k] for k in keys)
    if m.mode == "pct":
        return {k: baseline[k] * m.value / 100 * phase for k in keys}
    # M€ : réparti au prorata des lignes du groupe
    if total == 0:
        return {k: m.value * phase / len(keys) for k in keys}
    return {k: m.value * phase * baseline[k] / total for k in keys}


def project(base: BaseYear, a: Assumptions, measures: list[Measure] | None = None) -> list[YearResult]:
    """Projette `a.horizon` années après l'année de base.

    Retourne la liste des résultats, l'élément 0 étant l'année de base observée.
    Sans mesure, on obtient le scénario de référence (tendanciel).
    """
    measures = measures or []
    sp_groups: dict[str, list[str]] = {}
    for line in base.spending:
        if line.group:
            sp_groups.setdefault(line.group, []).append(line.key)
    rv_groups: dict[str, list[str]] = {}
    for line in base.revenue:
        if line.group:
            rv_groups.setdefault(line.group, []).append(line.key)

    sp_base = {l.key: l.amount for l in base.spending}
    rv_base = {l.key: l.amount for l in base.revenue}
    sp_trend = {l.key: l.trend for l in base.spending}

    first = YearResult(
        year=base.year,
        gdp=base.gdp,
        spending=sum(sp_base.values()),
        interest=base.interest,
        revenue=sum(rv_base.values()),
        deficit=sum(sp_base.values()) + base.interest - sum(rv_base.values()),
        debt=base.debt,
        apparent_rate=base.apparent_rate,
        spending_by_line=dict(sp_base),
        revenue_by_line=dict(rv_base),
    )
    results = [first]

    gdp_ref = base.gdp  # PIB tendanciel (sans effet multiplicateur)
    sp_ref = dict(sp_base)  # dépenses tendancielles par ligne
    rate = base.apparent_rate
    debt = base.debt

    for i in range(a.horizon):
        year = base.year + i + 1
        g = _at(a.gdp_growth, i)
        gdp_ref *= 1 + g
        for k in sp_ref:
            t = sp_trend[k] if sp_trend[k] is not None else _at(a.spending_trend, i)
            sp_ref[k] *= 1 + t
        rv_ref = {k: v * (gdp_ref / base.gdp) ** a.revenue_elasticity for k, v in rv_base.items()}

        sp = dict(sp_ref)
        rv = dict(rv_ref)
        for m in measures:
            if m.side == "spending":
                for k, d in _measure_delta(m, year, sp_ref, sp_groups).items():
                    sp[k] += d
            else:
                for k, d in _measure_delta(m, year, rv_ref, rv_groups).items():
                    rv[k] += d

        gdp = gdp_ref
        if a.multiplier_enabled:
            d_sp = sum(sp.values()) - sum(sp_ref.values())
            d_rv = sum(rv.values()) - sum(rv_ref.values())
            gdp = gdp_ref + a.spending_multiplier * d_sp - a.revenue_multiplier * d_rv
            # Effet de second tour : les recettes suivent le PIB effectif.
            ratio = (gdp / gdp_ref) ** a.revenue_elasticity
            rv = {k: v * ratio for k, v in rv.items()}

        # Intérêts : taux apparent (après refinancement partiel) × stock de début d'année.
        mr = _at(a.market_rate, i)
        rate = roll_rate(rate, mr, a.refinancing_share)
        interest = rate * debt
        deficit = sum(sp.values()) + interest - sum(rv.values())
        borrowing = deficit + _at(a.other_debt_flows, i)
        prev_debt = debt
        debt = prev_debt + borrowing
        # Les émissions nettes de l'année portent le taux de marché l'an prochain.
        next_rate = blend_rate(rate, prev_debt, borrowing, mr)

        results.append(
            YearResult(
                year=year,
                gdp=gdp,
                spending=sum(sp.values()),
                interest=interest,
                revenue=sum(rv.values()),
                deficit=deficit,
                debt=debt,
                apparent_rate=rate,
                spending_by_line=sp,
                revenue_by_line=rv,
            )
        )
        rate = next_rate
    return results


def compare(reference: list[YearResult], scenario: list[YearResult]) -> list[dict[str, float]]:
    """Écarts scénario - référence, année par année."""
    out = []
    for r, s in zip(reference, scenario, strict=True):
        out.append(
            {
                "year": s.year,
                "d_deficit": s.deficit - r.deficit,
                "d_debt": s.debt - r.debt,
                "d_interest": s.interest - r.interest,
                "d_debt_to_gdp_pts": (s.debt_to_gdp - r.debt_to_gdp) * 100,
            }
        )
    return out


# --- Recherche de l'effort nécessaire pour atteindre un objectif de dette ----------

Objectif = Literal["ratio_baisse", "ratio_cible", "ratio_base", "solde_equilibre"]


@dataclass(frozen=True)
class EffortResult:
    effort_meur: float  # effort annuel en régime de croisière (M€ courants), dépenses + recettes
    atteignable: bool
    measures: list[Measure]
    results: list[YearResult]
    valeur_atteinte: float  # valeur de l'indicateur visé à l'année cible


def effort_measures(effort: float, part_depenses: float, debut: int, montee: int) -> list[Measure]:
    """Effort réparti « au prorata » : baisse de toutes les dépenses et hausse de toutes
    les recettes (cible « * »), selon la part donnée aux dépenses."""
    out = []
    if part_depenses > 0:
        out.append(Measure("spending", "*", "meur", -effort * part_depenses, debut, montee))
    if part_depenses < 1:
        out.append(Measure("revenue", "*", "meur", effort * (1 - part_depenses), debut, montee))
    return out


def _indicateur(res: list[YearResult], objectif: str, annee: int) -> float:
    """Valeur à ramener sous le seuil : déficit, ratio, ou hausse du ratio sur un an."""
    i = next(i for i, r in enumerate(res) if r.year == annee)
    if objectif == "solde_equilibre":
        return res[i].deficit
    if objectif == "ratio_baisse":
        return res[i].debt_to_gdp - res[i - 1].debt_to_gdp
    return res[i].debt_to_gdp


def solve_effort(base: BaseYear, a: Assumptions, objectif: Objectif, *, annee: int, part_depenses: float = 0.5,
                 debut: int, montee: int = 1, cible: float | None = None, max_effort: float | None = None,
                 tol: float = 1.0) -> EffortResult:
    """Plus petit effort annuel (M€, en régime de croisière) qui atteint l'objectif à `annee` :

    - ratio_baisse : le ratio dette/PIB cesse de monter à `annee` (ratio N ≤ ratio N-1) ;
    - ratio_base : dette/PIB à `annee` revenu au niveau de l'année de base ;
    - ratio_cible : dette/PIB à `annee` ≤ `cible` (fraction, ex. 0.80) ;
    - solde_equilibre : déficit de l'État à `annee` ≤ 0 (la dette cesse de croître en euros).

    L'indicateur décroît avec l'effort (même avec le multiplicateur, tant que celui-ci
    reste < 1/élasticité) : recherche par dichotomie à `tol` M€ près.
    """
    if not 0 <= part_depenses <= 1:
        raise ValueError("part_depenses doit être entre 0 et 1")
    if annee > base.year + a.horizon or annee < debut or annee <= base.year:
        raise ValueError("l'année cible doit être dans l'horizon et après le début des mesures")
    seuil = {"ratio_baisse": 0.0, "ratio_base": base.debt / base.gdp, "ratio_cible": cible,
             "solde_equilibre": 0.0}[objectif]
    if seuil is None:
        raise ValueError("objectif ratio_cible : préciser la cible")

    def run(e: float) -> list[YearResult]:
        return project(base, a, effort_measures(e, part_depenses, debut, montee))

    hi = max_effort if max_effort is not None else sum(l.amount for l in base.spending) + sum(
        l.amount for l in base.revenue)  # borne haute : l'équivalent d'une année de budget
    if _indicateur(run(0.0), objectif, annee) <= seuil:
        res = run(0.0)
        return EffortResult(0.0, True, [], res, _indicateur(res, objectif, annee))
    if _indicateur(run(hi), objectif, annee) > seuil:
        res = run(hi)
        return EffortResult(hi, False, effort_measures(hi, part_depenses, debut, montee), res,
                            _indicateur(res, objectif, annee))
    lo = 0.0
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if _indicateur(run(mid), objectif, annee) <= seuil:
            hi = mid
        else:
            lo = mid
    res = run(hi)
    return EffortResult(hi, True, effort_measures(hi, part_depenses, debut, montee), res,
                        _indicateur(res, objectif, annee))
