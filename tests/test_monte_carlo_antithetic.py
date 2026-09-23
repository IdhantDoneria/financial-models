"""Regression test: with ``antithetic=True`` and an odd ``n_sims``, the model
must simulate and report exactly ``n_sims`` paths (previously it drew
``n_sims + 1`` paths -- one extra antithetic partner -- while still reporting
``n_sims``, see ``src/monte_carlo.py`` ``_simulate_terminal``).
"""

from __future__ import annotations

from src import MonteCarloOptionModel


def test_odd_n_sims_antithetic_reports_true_path_count():
    m = MonteCarloOptionModel(spot=100, strike=100, rate=0.05, sigma=0.2,
                              maturity=1.0, n_sims=1001, antithetic=True, seed=1)
    terminal = m._simulate_terminal(m.n_sims)
    assert terminal.size == 1001
    res = m.calculate()
    assert res["n_sims"] == 1001


def test_even_n_sims_antithetic_unaffected():
    m = MonteCarloOptionModel(spot=100, strike=100, rate=0.05, sigma=0.2,
                              maturity=1.0, n_sims=1000, antithetic=True, seed=1)
    terminal = m._simulate_terminal(m.n_sims)
    assert terminal.size == 1000
    res = m.calculate()
    assert res["n_sims"] == 1000


def test_odd_n_sims_non_antithetic_unaffected():
    m = MonteCarloOptionModel(spot=100, strike=100, rate=0.05, sigma=0.2,
                              maturity=1.0, n_sims=1001, antithetic=False, seed=1)
    terminal = m._simulate_terminal(m.n_sims)
    assert terminal.size == 1001
