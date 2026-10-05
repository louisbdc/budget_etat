"""Tests du moteur de projection.

Les montants utilisés sont des valeurs abstraites choisies pour que les
résultats se calculent à la main ; ce ne sont PAS des données budgétaires.
"""

import pytest

from budget_etat.projection import (
    Assumptions,
    BaseYear,
    Line,
    Measure,
    blend_rate,
    compare,
    project,
    roll_rate,
)


def base(**kw):
    defaults = dict(
        year=2000,
        gdp=1000.0,
        debt=500.0,
        interest=10.0,  # taux apparent 2 %
        spending=[Line("a", 60.0, group="M1"), Line("b", 40.0, group="M1"), Line("c", 100.0)],
        revenue=[Line("tva", 120.0), Line("ir", 80.0)],
    )
    defaults.update(kw)
    return BaseYear(**defaults)


FLAT = dict(gdp_growth=0.0, spending_trend=0.0, market_rate=0.02, refinancing_share=0.1)


# --- taux et refinancement -------------------------------------------------

def test_roll_rate_partial_refinancing():
    assert roll_rate(0.02, 0.04, 0.25) == pytest.approx(0.025)
    assert roll_rate(0.02, 0.04, 1.0) == pytest.approx(0.04)
    assert roll_rate(0.02, 0.04, 0.0) == pytest.approx(0.02)


def test_blend_rate_new_borrowing_at_market_rate():
    # 100 à 2 % + 100 émis à 4 % -> 3 %
    assert blend_rate(0.02, 100, 100, 0.04) == pytest.approx(0.03)


def test_blend_rate_repayment_keeps_rate():
    assert blend_rate(0.02, 100, -30, 0.04) == pytest.approx(0.02)


def test_base_year_apparent_rate():
    assert base().apparent_rate == pytest.approx(0.02)


# --- dynamique de la dette -------------------------------------------------

def test_first_row_is_observed_base_year():
    r = project(base(), Assumptions(horizon=1, **FLAT))[0]
    assert r.year == 2000
    assert r.deficit == pytest.approx(200 + 10 - 200)
    assert r.debt == 500


def test_debt_accumulates_deficit_including_interest():
    # Taux de marché = taux apparent -> taux constant à 2 %.
    res = project(base(), Assumptions(horizon=2, **FLAT))
    y1, y2 = res[1], res[2]
    assert y1.interest == pytest.approx(0.02 * 500)
    assert y1.deficit == pytest.approx(200 + 10 - 200)
    assert y1.debt == pytest.approx(510)
    assert y2.interest == pytest.approx(0.02 * 510)
    assert y2.debt == pytest.approx(510 + 10.2)


def test_interest_rate_shock_passes_through_progressively():
    a = Assumptions(horizon=3, gdp_growth=0.0, spending_trend=0.0, market_rate=0.06, refinancing_share=0.25)
    # Budget primaire équilibré et pas de dette nouvelle hors intérêts.
    b = base(spending=[Line("x", 200.0)], revenue=[Line("y", 200.0)])
    res = project(b, a)
    r1 = 0.75 * 0.02 + 0.25 * 0.06  # 3 %
    assert res[1].apparent_rate == pytest.approx(r1)
    assert res[1].interest == pytest.approx(r1 * 500)
    # L'an 2 intègre l'emprunt de l'an 1 (= intérêts) émis à 6 %, puis un roulement.
    debt1 = 500 + r1 * 500
    blended = (500 * r1 + r1 * 500 * 0.06) / debt1
    r2 = 0.75 * blended + 0.25 * 0.06
    assert res[2].apparent_rate == pytest.approx(r2)
    assert res[2].interest == pytest.approx(r2 * debt1)
    # Le taux converge vers le taux de marché sans le dépasser.
    assert res[1].apparent_rate < res[2].apparent_rate < res[3].apparent_rate < 0.06


def test_full_refinancing_jumps_to_market_rate():
    a = Assumptions(horizon=1, gdp_growth=0.0, spending_trend=0.0, market_rate=0.05, refinancing_share=1.0)
    assert project(base(), a)[1].interest == pytest.approx(0.05 * 500)


def test_other_debt_flows_change_debt_not_deficit():
    a = Assumptions(horizon=1, other_debt_flows=7.0, **FLAT)
    y1 = project(base(), a)[1]
    assert y1.deficit == pytest.approx(10)
    assert y1.debt == pytest.approx(517)


def test_surplus_reduces_debt():
    b = base(revenue=[Line("y", 260.0)])  # excédent primaire de 60
    y1 = project(b, Assumptions(horizon=1, **FLAT))[1]
    assert y1.deficit == pytest.approx(200 + 10 - 260)
    assert y1.debt == pytest.approx(450)
    assert y1.apparent_rate == pytest.approx(0.02)


# --- tendanciel ------------------------------------------------------------

def test_trend_growth_of_gdp_spending_and_revenue():
    a = Assumptions(horizon=1, gdp_growth=0.05, spending_trend=0.01, market_rate=0.02, refinancing_share=0.1)
    y1 = project(base(), a)[1]
    assert y1.gdp == pytest.approx(1050)
    assert y1.spending == pytest.approx(202)
    assert y1.revenue == pytest.approx(210)  # élasticité 1
    assert y1.debt_to_gdp == pytest.approx(y1.debt / 1050)


def test_revenue_elasticity():
    a = Assumptions(horizon=1, gdp_growth=0.10, revenue_elasticity=2.0, **{k: v for k, v in FLAT.items() if k != "gdp_growth"})
    assert project(base(), a)[1].revenue == pytest.approx(200 * 1.1**2)


def test_per_line_trend_overrides_default():
    b = base(spending=[Line("a", 100.0, trend=0.10), Line("c", 100.0)])
    y1 = project(b, Assumptions(horizon=1, **FLAT))[1]
    assert y1.spending_by_line["a"] == pytest.approx(110)
    assert y1.spending_by_line["c"] == pytest.approx(100)


def test_time_varying_assumptions():
    a = Assumptions(horizon=3, gdp_growth=[0.1, 0.0], spending_trend=0.0, market_rate=0.02, refinancing_share=0.1)
    res = project(base(), a)
    assert [round(r.gdp) for r in res] == [1000, 1100, 1100, 1100]  # dernière valeur prolongée


# --- mesures ---------------------------------------------------------------

def test_measure_ramp_up():
    m = Measure("spending", "c", "pct", -10.0, start_year=2002, ramp_years=4)
    assert [m.phase(y) for y in range(2001, 2007)] == [0, 0.25, 0.5, 0.75, 1, 1]


def test_measure_pct_on_line():
    m = Measure("spending", "c", "pct", -10.0, start_year=2001)
    y1 = project(base(), Assumptions(horizon=1, **FLAT), [m])[1]
    assert y1.spending_by_line["c"] == pytest.approx(90)
    assert y1.deficit == pytest.approx(0)


def test_measure_meur_on_group_split_pro_rata():
    m = Measure("spending", "M1", "meur", -10.0, start_year=2001)
    y1 = project(base(), Assumptions(horizon=1, **FLAT), [m])[1]
    assert y1.spending_by_line["a"] == pytest.approx(54)
    assert y1.spending_by_line["b"] == pytest.approx(36)


def test_revenue_measure():
    m = Measure("revenue", "ir", "meur", 5.0, start_year=2001)
    y1 = project(base(), Assumptions(horizon=1, **FLAT), [m])[1]
    assert y1.revenue == pytest.approx(205)


def test_unknown_target_raises():
    m = Measure("spending", "nope", "pct", -1.0, start_year=2001)
    with pytest.raises(KeyError):
        project(base(), Assumptions(horizon=1, **FLAT), [m])


def test_savings_compound_through_interest():
    m = Measure("spending", "c", "meur", -10.0, start_year=2001)
    a = Assumptions(horizon=2, **FLAT)
    ref, sc = project(base(), a), project(base(), a, [m])
    d = compare(ref, sc)
    assert d[1]["d_deficit"] == pytest.approx(-10)
    assert d[1]["d_debt"] == pytest.approx(-10)
    # An 2 : -10 de dépense, -0,2 d'intérêts sur la dette évitée (taux 2 %).
    assert d[2]["d_interest"] == pytest.approx(-0.2)
    assert d[2]["d_debt"] == pytest.approx(-20.2)


# --- multiplicateur --------------------------------------------------------

def test_multiplier_disabled_by_default():
    m = Measure("spending", "c", "meur", -10.0, start_year=2001)
    a = Assumptions(horizon=1, spending_multiplier=1.0, **FLAT)
    assert project(base(), a, [m])[1].gdp == pytest.approx(1000)


def test_multiplier_reduces_gdp_and_revenue():
    m = Measure("spending", "c", "meur", -10.0, start_year=2001)
    a = Assumptions(horizon=1, multiplier_enabled=True, spending_multiplier=0.5, **FLAT)
    y1 = project(base(), a, [m])[1]
    assert y1.gdp == pytest.approx(995)
    assert y1.revenue == pytest.approx(200 * 0.995)
    # L'économie nette sur le déficit est inférieure à 10.
    assert y1.deficit == pytest.approx(190 + 10 - 199)


def test_measure_on_all_lines():
    m = Measure("spending", "*", "meur", -20.0, start_year=2001)
    y1 = project(base(), Assumptions(horizon=1, **FLAT), [m])[1]
    # 200 de dépenses (a 60, b 40, c 100) : -20 réparti au prorata
    assert y1.spending_by_line == pytest.approx({"a": 54, "b": 36, "c": 90})
