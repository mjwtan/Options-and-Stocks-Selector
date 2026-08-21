# QuantLib Risk Analytics Engine — Specification

**Purpose:** Replace the analytic portfolio-volatility step in position sizing with a Monte Carlo risk engine built on QuantLib. Size the book to a CVaR target rather than a volatility target, and add scenario and stress analysis.

**Why:** volatility targeting assumes returns are approximately normal. They are not, and the divergence is worst in the tails — which is the part that matters. CVaR targeting makes no such assumption. Secondarily, this is a compute-bound workload suitable for performance optimisation, unlike the analytic formula it replaces.

**Where it plugs in:** §6 of the position sizing spec. Everything upstream (rank-based raw weights, constraints) and downstream (regime scalar, order generation) is unchanged.

---

## 1. What changes

Current:

```
sigma_p = sqrt(w.T @ Sigma @ w)
k_vol   = min(1.0, SIGMA_TARGET / sigma_p)
```

Replacement:

```
paths   = simulate_portfolio(w, Sigma, mu, horizon, n_paths)
cvar    = conditional_value_at_risk(paths, alpha=0.95)
k_risk  = min(1.0, CVAR_TARGET / cvar)
```

Both produce a scalar in (0, 1] that scales the whole book. The rest of the pipeline does not care which produced it, so this is a contained substitution.

Keep the analytic path in the codebase behind a flag (`--risk-engine analytic|montecarlo`). It is the reference implementation for validating the MC engine, and a fallback if the simulation fails.

---

## 2. Dependencies

```
pip install QuantLib numpy scikit-learn
```

The Python package is `QuantLib` (capitalised). Bindings are SWIG-generated, so the API is C++-shaped — `ql.Date` does not interoperate with `datetime` or pandas timestamps. Confine all QuantLib usage to the modules below and convert at the boundary; do not scatter `ql.` calls through the codebase.

---

## 3. Module layout

```
risk/
  __init__.py
  calendar.py           # QuantLib date/calendar wrapper
  covariance.py         # Ledoit-Wolf estimation, Cholesky decomposition
  montecarlo.py         # QuantLib path generation
  measures.py           # VaR, CVaR, drawdown from simulated paths
  scenarios.py          # deterministic stress tests
  engine.py             # orchestration; the only module sizing imports
benchmarks/
  bench_mc.py           # throughput harness
  fixtures/             # fixed-seed reference outputs
```

`engine.py` exposes one function to the sizing code:

```python
def compute_risk_scalar(weights, returns_matrix, config) -> RiskResult
```

`RiskResult` carries `k_risk`, `cvar`, `var`, `sigma_p_analytic` (for comparison), `n_paths`, `seed`, and `elapsed_ms`.

---

## 4. Covariance input

Unchanged from the existing spec, and still required:

```python
from sklearn.covariance import LedoitWolf

Sigma_daily = LedoitWolf().fit(returns_matrix).covariance_
Sigma_ann   = Sigma_daily * 252
```

QuantLib has no shrinkage estimator; with 20 assets and 250 observations a raw sample covariance is unstable, so sklearn does this step.

**Cholesky decomposition** for correlated path generation — use QuantLib's, which handles near-singular matrices more gracefully than a naive `numpy.linalg.cholesky`:

```python
ql_matrix = ql.Matrix(n, n)
# populate from Sigma_daily
L = ql.pseudoSqrt(ql_matrix, ql.SalvagingAlgorithm.Spectral)
```

`SalvagingAlgorithm.Spectral` repairs matrices that are not positive semi-definite by flooring negative eigenvalues at zero. This will happen occasionally with real data; without salvaging, the run crashes.

Log whenever salvaging alters the matrix materially — it indicates a data problem worth investigating.

---

## 5. Path generation

### 5.1 Random sequence

Use Sobol low-discrepancy sequences rather than pseudo-random. They converge faster for this class of problem, meaning fewer paths for equivalent accuracy — which matters directly for the performance target.

```python
dimension = n_assets * n_steps
rsg = ql.SobolRsg(dimension, seed)
gsg = ql.GaussianLowDiscrepancySequenceGenerator(
    ql.UniformLowDiscrepancySequenceGenerator(dimension, seed)
)
```

Also implement the Mersenne Twister path (`ql.GaussianRandomSequenceGenerator` over `ql.UniformRandomSequenceGenerator`) behind a config flag. It is needed as a convergence cross-check: if Sobol and MT disagree beyond Monte Carlo error, the implementation is wrong.

### 5.2 Return model

Multivariate geometric Brownian motion. Per step, per path:

```
z        ~ correlated standard normals (L @ independent_normals)
r_t      = (mu - 0.5 * sigma^2) * dt + sigma * sqrt(dt) * z
```

**Set `mu = 0` by default.** Expected-return estimates are unreliable at this horizon and inject a directional view into what should be a risk measurement. Make it configurable but default it off, and document that non-zero `mu` means the CVaR figure is no longer a pure risk number.

`dt = 1/252` for daily steps.

### 5.3 Horizon and path count

| Parameter | Default | Notes |
|---|---|---|
| `MC_HORIZON_DAYS` | 5 | One rebalance period |
| `MC_N_PATHS` | 50000 | See §9 for convergence testing |
| `MC_SEED` | 42 | Fixed for reproducibility |
| `MC_ANTITHETIC` | True | Variance reduction |

Antithetic variates roughly halve the paths needed for a given standard error. Cheap to implement, meaningful gain.

---

## 6. Risk measures

From the terminal portfolio returns across all paths:

```python
losses = -portfolio_returns          # positive = loss

var_95  = np.percentile(losses, 95)
cvar_95 = losses[losses >= var_95].mean()
```

**CVaR (expected shortfall)** is the average loss in the worst 5% of paths. Prefer it over VaR: it is coherent as a risk measure (VaR is not — it fails subadditivity), and it responds to tail shape rather than a single quantile.

Scale to annualised terms for comparability with the volatility target:

```python
cvar_annualised = cvar_95 * sqrt(252 / MC_HORIZON_DAYS)
```

Also compute, for logging only:

- Maximum path drawdown, mean and 95th percentile
- Portfolio volatility from the simulated paths — this must agree closely with `sqrt(wᵀΣw)`; a divergence is a bug, not a finding

---

## 7. Sizing from CVaR

```python
k_risk = min(1.0, CVAR_TARGET / cvar_annualised)
w_final = w_constrained * k_risk * k_regime
```

`CVAR_TARGET` default **0.25** (25% annualised expected shortfall). This is roughly comparable in aggressiveness to a 0.15 volatility target under normal returns, but will scale down harder when the book has fat-tailed or highly correlated holdings.

Cap at 1.0 — never lever.

**Calibrate before trusting it.** Run both engines over several months of historical weights and compare `k_vol` against `k_risk`. If `k_risk` is systematically lower, the target is too tight and the book will sit in cash. Tune `CVAR_TARGET` so the two produce similar average exposure in normal conditions — then the difference between them shows up only in stressed conditions, which is the point.

---

## 8. Scenario analysis

Separate from the MC engine, deterministic, logged but not acted on initially.

Revalue the portfolio under fixed shocks:

| Scenario | Shock |
|---|---|
| Broad selloff | All assets −10%, correlations → 0.9 |
| Vol spike | All volatilities × 2 |
| Correlation breakdown | Off-diagonal correlations → 0.95 |
| Sector rotation | Largest sector −15%, others +2% |
| Historical: 2020-03 | Replay 20 trading days from 2020-02-19 |
| Historical: 2022 drawdown | Replay worst 20-day window |

Report portfolio return under each. The correlation scenarios matter most — they answer "what happens when diversification stops working," which is precisely when it is needed.

Historical replays require aligning your current tickers to that period; names without history are excluded and flagged rather than substituted.

---

## 9. Correctness validation

The MC engine must be validated before it drives sizing. Required tests:

**Convergence.** As `n_paths` increases (1k, 5k, 10k, 50k, 100k), CVaR converges and the standard error shrinks as `1/sqrt(n)`. Plot it. If the error does not scale that way, the sampling is wrong.

**Analytic agreement.** With `mu = 0` and normal returns, simulated portfolio volatility must match `sqrt(wᵀΣw)` within Monte Carlo error. This is the single most important test — it validates the correlation structure end to end.

**Normal-case CVaR.** For a normal distribution, CVaR at 95% has a closed form: `sigma * phi(z_95) / 0.05`. The simulated value must match it. Deviation means the tail sampling is broken.

**Sobol vs. Mersenne Twister.** Both generators must converge to the same value. Disagreement beyond MC error indicates a dimension-ordering or seeding bug.

**Determinism.** Same seed and inputs produce byte-identical output.

**Degenerate inputs.** Single asset; two perfectly correlated assets; a non-PSD covariance matrix — each must produce a sensible result or a clear error, not a crash or silent garbage.

---

## 10. Performance harness

`benchmarks/bench_mc.py` is a deliverable in its own right, not an afterthought. Without it there is nothing to optimise against and no way to demonstrate improvement.

It must report:

- Wall time and paths/second, at fixed `n_paths` and fixed seed
- Peak memory
- CVaR value produced (to assert correctness is preserved)
- Breakdown by phase: covariance estimation, Cholesky, path generation, measure computation

Run each configuration at least 5 times and report median and interquartile range — single timings are noise.

**Baseline first.** Record the current implementation's numbers before any optimisation, and commit the fixture. Every subsequent change is measured against it.

**The correctness assertion is non-negotiable:** any optimisation must reproduce the baseline CVaR to within a stated tolerance (suggest 1e-6 relative for deterministic changes, or within MC standard error for changes that alter sampling). A speedup that changes the answer is not a speedup.

Likely hot spots, in expected order: path generation inner loop, RNG throughput, matrix allocation inside loops, and whether the accumulation vectorises.

---

## 11. Configuration

| Name | Default | Notes |
|---|---|---|
| `RISK_ENGINE` | `montecarlo` | or `analytic` |
| `CVAR_TARGET` | 0.25 | Annualised, calibrate per §7 |
| `CVAR_ALPHA` | 0.95 | Confidence level |
| `MC_N_PATHS` | 50000 | |
| `MC_HORIZON_DAYS` | 5 | |
| `MC_SEED` | 42 | |
| `MC_ANTITHETIC` | True | |
| `MC_GENERATOR` | `sobol` | or `mersenne` |
| `MC_DRIFT` | 0.0 | Leave at zero; see §5.2 |
| `COV_WINDOW_DAYS` | 250 | |

CLI overrides: `--risk-engine`, `--mc-paths`, `--mc-seed`, `--risk-dry-run` (compute and log without applying).

---

## 12. Logging

Per run:

- `k_risk`, `cvar_annualised`, `var_95`
- `sigma_p` from simulation and from the analytic formula, plus the difference
- `n_paths`, `seed`, `generator`, `elapsed_ms`
- Whether Cholesky salvaging altered the covariance matrix
- Scenario results from §8
- Which risk engine produced the scalar actually applied

The simulated-versus-analytic volatility comparison should be logged every single run, not just in tests. It is a continuous correctness check that costs nothing.

---

## 13. Build order

1. `calendar.py` and `covariance.py` — no QuantLib in the second, just sklearn plus the Cholesky wrapper.
2. `montecarlo.py` with Mersenne Twister only. Simpler to debug.
3. `measures.py` and the §9 validation tests. **Do not proceed until analytic agreement passes.**
4. Sobol generator and antithetic variates; re-run convergence tests.
5. `engine.py`, wired behind `RISK_ENGINE=analytic` by default.
6. `benchmarks/bench_mc.py`; record the baseline.
7. Calibrate `CVAR_TARGET` per §7 against historical weights.
8. Switch the default to `montecarlo` only after steps 3 and 7 both pass.
9. `scenarios.py` — log-only initially.

---

## 14. A note on scope

Monte Carlo risk is more machinery than a 20-stock long-only book strictly requires; the analytic formula would serve adequately. It is justified here by two things: CVaR handles tail risk in a way volatility targeting cannot, which matters for a portfolio deliberately selecting volatile names, and it provides a genuine compute-bound workload.

Both are legitimate. But keep the analytic engine working and comparable throughout — if the two consistently produce similar scalars in live conditions, that is worth knowing, and it is an argument for the simpler path rather than a disappointment.
