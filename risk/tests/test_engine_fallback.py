"""system-spec.md S15.5: "MC engine fails or exceeds time budget -> Fall
back to RISK_ENGINE=analytic, log loudly." Confirms compute_risk_scalar
degrades gracefully rather than propagating the exception and crashing
the whole run.
"""

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from risk.engine import RiskConfig, compute_risk_scalar


@pytest.fixture
def inputs():
    weights = pd.Series([0.5, 0.5], index=["A", "B"])
    Sigma = np.array([[0.04, 0.01], [0.01, 0.04]])
    return weights, Sigma


def test_montecarlo_failure_falls_back_to_analytic(inputs):
    weights, Sigma = inputs
    cfg = RiskConfig(risk_engine="montecarlo", sigma_target=0.15)

    with patch("risk.engine.simulate_portfolio", side_effect=RuntimeError("simulated MC failure")):
        result = compute_risk_scalar(weights, Sigma, cfg)

    assert result.risk_engine_used == "analytic"
    assert result.fallback_reason is not None
    assert "simulated MC failure" in result.fallback_reason
    # k_risk still computed correctly via the analytic formula, not None/garbage
    expected_sigma_p = float(np.sqrt(weights.values @ Sigma @ weights.values))
    assert result.k_risk == pytest.approx(min(1.0, cfg.sigma_target / expected_sigma_p))


def test_analytic_engine_has_no_fallback_reason(inputs):
    weights, Sigma = inputs
    cfg = RiskConfig(risk_engine="analytic")
    result = compute_risk_scalar(weights, Sigma, cfg)
    assert result.fallback_reason is None


def test_successful_montecarlo_has_no_fallback_reason(inputs):
    weights, Sigma = inputs
    cfg = RiskConfig(risk_engine="montecarlo", mc_n_paths=1000)
    result = compute_risk_scalar(weights, Sigma, cfg)
    assert result.risk_engine_used == "montecarlo"
    assert result.fallback_reason is None
