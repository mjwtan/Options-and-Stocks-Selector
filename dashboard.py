"""
Streamlit dashboard - mostly a read-only view over everything this system
already logs (account/positions, run logs, validation ledger, performance
summary, daily monitor alerts, heartbeats, GitHub Actions status).

One exception, by explicit user choice: the "Rebalance to target" section
on the Positions page can submit real (paper) orders. It reuses
trade_from_csv.py's own planning/execution functions rather than
reimplementing order logic, and is gated behind a mandatory review
checkbox that resets after every execution - the checkbox and the button
are the only thing standing between clicking around this page and a real
order hitting the paper account, so neither should be made more
convenient than they already are. Every execution is logged to
logs/dashboard_execution_*.json for the same reason run_*.json exists -
retrofitting an audit trail after the fact is impossible.

Usage:
    streamlit run dashboard.py
"""

from __future__ import annotations

import glob
import io
import json
import os
import re
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from dotenv import load_dotenv

from brokers.alpaca_equity import AlpacaEquityBroker

load_dotenv()

BASE_DIR = Path(__file__).parent
HISTORY_DIR = BASE_DIR / "history"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR = BASE_DIR / "state"

HEARTBEAT_JOBS = ["weekly_sizing", "daily_monitor", "report"]
HEARTBEAT_MAX_AGE_DAYS = 8

# Fixed categorical order (Okabe-Ito - colorblind-safe), assigned by
# entity identity, never by rank or cycled in from a generic palette.
COLOR_ACTUAL = "#0072B2"       # blue
COLOR_EQUAL_WEIGHT = "#E69F00"  # orange
COLOR_BENCHMARK = "#009E73"     # bluish green
COLOR_K_VOL = "#0072B2"
COLOR_K_RISK = "#D55E00"        # vermillion
COLOR_SEQUENTIAL = "#0072B2"    # single hue, magnitude only (position weights)
STATUS_WARNING = "#B45309"      # amber - reserved for severity, never reused as a categorical color
STATUS_INFO = "#64748B"         # slate

st.set_page_config(page_title="Options Selector Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Data loading - all read-only, all tolerant of "not enough data yet"
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def load_account_and_positions():
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        return None, [], None
    broker = AlpacaEquityBroker(api_key, secret_key, paper=True)
    account = broker.account()
    positions = broker.positions()

    option_positions = []
    try:
        from brokers.alpaca_options import AlpacaOptionsBroker
        options_broker = AlpacaOptionsBroker(api_key, secret_key, paper=True)
        option_positions = options_broker.option_positions()
    except Exception:
        pass  # equity-only setups shouldn't error just because options aren't configured

    return account, positions, option_positions


def load_regime_state():
    path = STATE_DIR / "regime_state.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def load_latest_run_log():
    runs = sorted(LOG_DIR.glob("run_*.json"))
    if not runs:
        return None, None
    latest = runs[-1]
    try:
        return json.loads(latest.read_text()), latest
    except (json.JSONDecodeError, OSError):
        return None, None


def load_ledger():
    path = HISTORY_DIR / "validation_ledger.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["run_date"] = pd.to_datetime(df["run_date"])
    return df.sort_values("run_date")


def load_per_name_ledger():
    path = HISTORY_DIR / "validation_ledger_per_name.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["run_date"] = pd.to_datetime(df["run_date"])
    return df.sort_values("run_date")


def load_performance_summary():
    path = HISTORY_DIR / "performance_summary.csv"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def get_github_repo_slug():
    try:
        url = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"], cwd=BASE_DIR, text=True, timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


@st.cache_data(ttl=120)
def load_github_actions_status():
    """Latest run per workflow, via the public unauthenticated API - fine
    for a public repo (no token needed) and cached for 2 minutes so a busy
    dashboard session doesn't get near the 60 req/hour unauthenticated
    rate limit."""
    slug = get_github_repo_slug()
    if slug is None:
        return None, None
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{slug}/actions/runs", params={"per_page": 30}, timeout=10,
        )
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
    except (requests.RequestException, ValueError):
        return slug, None

    latest_by_workflow = {}
    for run in runs:
        name = run.get("name", "unknown")
        if name not in latest_by_workflow:
            latest_by_workflow[name] = run
    return slug, latest_by_workflow


def load_recent_alerts(days=14):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for path in sorted(glob.glob(str(LOG_DIR / "monitor_*.json"))):
        try:
            data = json.loads(Path(path).read_text())
            run_at = datetime.fromisoformat(data["run_at"])
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            continue
        if run_at < cutoff:
            continue
        for alert in data.get("alerts", []):
            rows.append({"run_at": run_at, **alert})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("run_at", ascending=False)


def one_axis_line_chart(df, x, series: dict, title, y_title):
    """series: {column_name: color}. One y-axis, always - see dataviz
    anti-patterns on dual-axis charts. A legend is always shown for >=2
    series; hover is on by default via Plotly."""
    fig = go.Figure()
    for col, color in series.items():
        if col not in df.columns:
            continue
        fig.add_trace(go.Scatter(
            x=df[x], y=df[col], mode="lines+markers", name=col,
            line=dict(color=color, width=2), marker=dict(size=6),
        ))
    fig.update_layout(
        title=title, yaxis_title=y_title, xaxis_title=None,
        height=320, margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
    )
    return fig


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def render_overview():
    account, positions, option_positions = load_account_and_positions()
    regime = load_regime_state()
    run_log, run_log_path = load_latest_run_log()

    if account is None:
        st.warning("ALPACA_API_KEY / ALPACA_SECRET_KEY not set - can't reach the account.")
        return

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Equity", f"${account.equity:,.0f}")
    col2.metric("Cash", f"${account.cash:,.0f}", f"{account.cash / account.equity:.1%} of equity")
    col3.metric("Buying power", f"${account.buying_power:,.0f}")
    col4.metric("Positions", f"{len(positions)} equity, {len(option_positions)} option")

    col1, col2, col3 = st.columns(3)
    if regime:
        col1.metric("k_regime", f"{regime['k_regime']:.2f}")
        col2.metric("Benchmark used", regime.get("benchmark_used", "-"))
    else:
        col1.metric("k_regime", "no state yet")
    if run_log_path:
        run_at = datetime.fromisoformat(run_log["run_at"])
        col3.metric("Last sizing run", run_at.strftime("%Y-%m-%d %H:%M UTC"))
    else:
        col3.metric("Last sizing run", "never")


def render_positions():
    account, positions, option_positions = load_account_and_positions()
    if account is None:
        st.warning("Can't reach the account.")
        return

    st.subheader("Equity positions")
    if not positions:
        st.info("No open equity positions.")
    else:
        df = pd.DataFrame([
            {"ticker": p.symbol, "market_value": p.market_value,
             "weight_pct": p.market_value / account.equity * 100,
             "unrealized_plpc": p.unrealized_plpc}
            for p in positions
        ]).sort_values("weight_pct", ascending=True)

        fig = go.Figure(go.Bar(
            x=df["weight_pct"], y=df["ticker"], orientation="h",
            marker=dict(color=COLOR_SEQUENTIAL),
            hovertemplate="%{y}: %{x:.1f}%<extra></extra>",
        ))
        fig.update_layout(
            title="Weight by position (% of equity)", xaxis_title="% of equity",
            height=max(320, 24 * len(df)), margin=dict(l=10, r=10, t=40, b=10),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)

        display = df.sort_values("weight_pct", ascending=False).copy()
        display["market_value"] = display["market_value"].map(lambda v: f"${v:,.0f}")
        display["weight_pct"] = display["weight_pct"].map(lambda v: f"{v:.1f}%")
        display["unrealized_plpc"] = display["unrealized_plpc"].map(
            lambda v: f"{v:+.1%}" if pd.notna(v) else "n/a"
        )
        st.dataframe(display, use_container_width=True, hide_index=True)

    st.subheader("Option positions")
    if not option_positions:
        st.info("No open option positions.")
    else:
        opt_df = pd.DataFrame([
            {"underlying": p.underlying, "type": p.contract_type, "strike": p.strike,
             "expiry": p.expiry, "qty": p.qty, "side": "short" if p.qty < 0 else "long",
             "market_value": p.market_value}
            for p in option_positions
        ])
        st.dataframe(opt_df, use_container_width=True, hide_index=True)

    st.divider()
    render_rebalance_and_approve(account, positions, option_positions)


def render_rebalance_and_approve(account, positions, option_positions):
    st.subheader("Rebalance to target_positions.csv")

    csv_path = BASE_DIR / "target_positions.csv"
    if not csv_path.exists():
        st.info("No target_positions.csv found - run position_sizing.py first.")
        return

    from trade_from_csv import load_option_targets, load_targets, plan_actions, plan_option_actions

    try:
        targets = load_targets(str(csv_path))
    except (ValueError, OSError) as e:
        st.error(f"Couldn't read target_positions.csv: {e}")
        return

    options_enabled = st.checkbox("Include option targets (short puts / long calls) from this CSV", value=True)
    option_targets = load_option_targets(str(csv_path)) if options_enabled else {}

    current_values = {p.symbol: p.market_value for p in positions}
    actions = plan_actions(targets, current_values, account.equity)
    close_occ, open_targets = plan_option_actions(option_targets, option_positions or [])

    if not actions and not close_occ and not open_targets:
        st.success("Nothing to do - positions already match targets (within the no-trade band).")
        return

    if actions:
        plan_df = pd.DataFrame(actions, columns=["ticker", "action", "current_$", "target_$", "order_$"])
        for c in ["current_$", "target_$", "order_$"]:
            plan_df[c] = plan_df[c].map(lambda v: f"${v:,.2f}")
        st.dataframe(plan_df, use_container_width=True, hide_index=True)
    if close_occ or open_targets:
        st.caption("Option changes:")
        opt_rows = [{"occ_symbol": occ, "action": "close"} for occ in close_occ] + [
            {"occ_symbol": t.occ_symbol, "action": f"open {t.instrument}", "contracts": t.contracts}
            for t in open_targets
        ]
        st.dataframe(pd.DataFrame(opt_rows), use_container_width=True, hide_index=True)

    st.warning(
        "Approving submits real (paper) orders to Alpaca immediately - equity sells/closes first, "
        "then confirmed equity buys, then option orders, same order trade_from_csv.py uses."
    )
    confirm_key = "approve_confirm_checkbox"
    confirmed = st.checkbox("I have reviewed this plan and want to submit these orders now.", key=confirm_key)
    if st.button("Approve & Execute", type="primary", disabled=not confirmed):
        execute_approved_plan(account, actions, option_targets, close_occ, open_targets, option_positions)
        st.session_state[confirm_key] = False  # force a fresh review before another execution


def execute_approved_plan(account, actions, option_targets, close_occ, open_targets, current_option_positions):
    from brokers.alpaca_options import AlpacaOptionsBroker
    from trade_from_csv import execute_actions

    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    equity_broker = AlpacaEquityBroker(api_key, secret_key, paper=True)

    options_broker = None
    existing_reserved_capital = 0.0
    if option_targets:
        try:
            options_broker = AlpacaOptionsBroker(api_key, secret_key, paper=True)
            existing_reserved_capital = float(options_broker.buying_power_reserved())
        except Exception as e:
            st.error(f"Couldn't set up the options broker ({e}) - option orders will be skipped this run.")
            options_broker = None

    buf = io.StringIO()
    with st.spinner("Submitting orders and waiting for fill confirmation..."):
        with redirect_stdout(buf):
            execute_actions(
                equity_broker, actions,
                options_broker=options_broker, close_option_occ=close_occ, open_option_targets=open_targets,
                current_option_positions=current_option_positions or [],
                account_buying_power=float(account.buying_power) if options_broker is not None else None,
                existing_reserved_capital=existing_reserved_capital,
            )
    output = buf.getvalue()
    st.code(output or "(nothing was submitted)")

    log_path = LOG_DIR / f"dashboard_execution_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    log_path.write_text(json.dumps({
        "run_at": datetime.now(timezone.utc).isoformat(),
        "source": "dashboard.py approve-and-execute",
        "equity_actions": actions,
        "option_close": close_occ,
        "option_open": [t.occ_symbol for t in open_targets],
        "output": output,
    }, indent=2, default=str))
    st.caption(f"Logged to {log_path.relative_to(BASE_DIR)}")

    load_account_and_positions.clear()
    st.success("Execution finished. Reload or switch tabs to see updated positions.")


def render_latest_decision():
    run_log, run_log_path = load_latest_run_log()
    if run_log is None:
        st.info("No run log found yet - run position_sizing.py first.")
        return

    run_at = datetime.fromisoformat(run_log["run_at"])
    st.caption(f"From {run_log_path.name} - {run_at.strftime('%Y-%m-%d %H:%M UTC')}")

    risk = run_log["risk"]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("k_regime", f"{run_log['regime']['k_regime']:.2f}")
    col2.metric("k_risk / k_vol", f"{risk['k_risk']:.2f}", risk["risk_engine_used"])
    col3.metric("Cash", f"{run_log['cash_pct']:.1%}")
    if risk.get("cvar_annualised") is not None:
        col4.metric("CVaR (annualised)", f"{risk['cvar_annualised']:.1%}")

    instrument_decisions = run_log.get("instrument_decisions", {})
    skip_decisions = run_log.get("skip_decisions", {})

    if instrument_decisions:
        counts = pd.Series([d["instrument"] for d in instrument_decisions.values()]).value_counts()
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Shares", int(counts.get("shares", 0)))
        col2.metric("Long calls", int(counts.get("long_call", 0)))
        col3.metric("Short puts", int(counts.get("short_put", 0)))
        col4.metric("Skipped", len(skip_decisions))

    st.subheader("Per-name decisions")
    weight_final = run_log.get("weight_final", {})
    rows = []
    for ticker, weight in weight_final.items():
        d = instrument_decisions.get(ticker, {})
        rows.append({
            "ticker": ticker, "weight": weight, "instrument": d.get("instrument", "-"),
            "reason": d.get("reason", "-"),
        })
    for ticker, s in skip_decisions.items():
        rows.append({"ticker": ticker, "weight": 0.0, "instrument": f"SKIP ({s['gate']})", "reason": s["reason"]})

    if rows:
        df = pd.DataFrame(rows).sort_values("weight", ascending=False)
        df["weight"] = df["weight"].map(lambda v: f"{v:.1%}")
        st.dataframe(df, use_container_width=True, hide_index=True)


def render_performance():
    perf = load_performance_summary()
    if perf.empty:
        st.info("No performance data yet - run track_performance.py after at least one weekly sizing run.")
        return

    st.subheader("Return since entry, by week (equity positions only)")
    display = perf.copy()
    for c in ["actual_return", "equal_weight_return", "benchmark_return", "vs_equal_weight", "vs_benchmark"]:
        if c in display.columns:
            display[c] = display[c].map(lambda v: f"{v:+.2%}" if pd.notna(v) else "n/a")
    st.dataframe(display, use_container_width=True, hide_index=True)

    chart_df = perf.dropna(subset=["actual_return"]).sort_values("week")
    if len(chart_df) >= 2:
        fig = one_axis_line_chart(
            chart_df, "week",
            {"actual_return": COLOR_ACTUAL, "equal_weight_return": COLOR_EQUAL_WEIGHT, "benchmark_return": COLOR_BENCHMARK},
            "Actual vs. equal-weight vs. benchmark", "Return",
        )
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("Need at least 2 weeks with a computed return to chart a trend.")


def render_ledger_trends():
    ledger = load_ledger()
    if ledger.empty:
        st.info("No validation ledger rows yet - run position_sizing.py first.")
        return

    st.dataframe(ledger, use_container_width=True, hide_index=True)

    if len(ledger) < 2:
        st.caption("Need at least 2 weekly rows to chart a trend.")
        return

    col1, col2 = st.columns(2)
    with col1:
        fig = one_axis_line_chart(ledger, "run_date", {"portfolio_value": COLOR_ACTUAL}, "Portfolio value", "$")
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        fig = one_axis_line_chart(ledger, "run_date", {"cash_pct": COLOR_ACTUAL}, "Cash %", "Fraction")
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(fig, use_container_width=True)

    col1, col2 = st.columns(2)
    with col1:
        fig = one_axis_line_chart(
            ledger, "run_date", {"k_vol": COLOR_K_VOL, "k_risk": COLOR_K_RISK}, "k_vol vs k_risk", "Scalar",
        )
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        fig = one_axis_line_chart(ledger, "run_date", {"k_regime": COLOR_ACTUAL}, "k_regime", "Scalar")
        st.plotly_chart(fig, use_container_width=True)

    if "turnover_pct" in ledger.columns and ledger["turnover_pct"].notna().any():
        fig = one_axis_line_chart(ledger, "run_date", {"turnover_pct": COLOR_ACTUAL}, "Turnover", "Fraction of portfolio")
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(fig, use_container_width=True)


def render_alerts():
    alerts = load_recent_alerts(days=14)
    if alerts.empty:
        st.info("No alerts in the last 14 days (or daily_monitor.py hasn't run yet).")
        return

    n_warnings = (alerts["severity"] == "warning").sum()
    st.metric("Warnings in the last 14 days", int(n_warnings))

    display = alerts.copy()
    display["run_at"] = display["run_at"].dt.strftime("%Y-%m-%d %H:%M")

    def _style_severity(val):
        color = STATUS_WARNING if val == "warning" else STATUS_INFO
        return f"color: {color}; font-weight: 600"

    st.dataframe(
        display[["run_at", "severity", "category", "symbol", "message"]].style.map(
            _style_severity, subset=["severity"]
        ),
        use_container_width=True, hide_index=True,
    )


def render_automation():
    from heartbeat import HEARTBEAT_PATH, check_heartbeats

    st.subheader("Heartbeats")
    st.caption("Recorded by weekly_sizing / daily_monitor / report each time they complete successfully.")
    data = {}
    if HEARTBEAT_PATH.exists():
        try:
            data = json.loads(HEARTBEAT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    stale = check_heartbeats(HEARTBEAT_JOBS, max_age_days=HEARTBEAT_MAX_AGE_DAYS)

    cols = st.columns(len(HEARTBEAT_JOBS))
    for col, job in zip(cols, HEARTBEAT_JOBS):
        last = data.get(job)
        if last is None:
            col.metric(job, "never run")
            continue
        try:
            dt = datetime.fromisoformat(last)
        except ValueError:
            col.metric(job, "unparseable timestamp")
            continue
        age_days = (datetime.now(timezone.utc) - dt).days
        label = f"{age_days}d ago" + (" — STALE" if job in stale else "")
        col.metric(job, dt.strftime("%Y-%m-%d %H:%M UTC"), label, delta_color="inverse" if job in stale else "off")

    st.divider()
    st.subheader("GitHub Actions")
    slug, runs_by_workflow = load_github_actions_status()
    if slug is None:
        st.info("No GitHub remote found for this repo - can't look up workflow status.")
    elif runs_by_workflow is None:
        st.info(f"Couldn't reach the GitHub Actions API for {slug}.")
    elif not runs_by_workflow:
        st.info(f"{slug}: no workflow runs yet.")
    else:
        st.caption(f"github.com/{slug}")
        rows = [
            {
                "workflow": name,
                "status": run.get("status"),
                "conclusion": run.get("conclusion") or "-",
                "last_run": run.get("created_at"),
                "url": run.get("html_url"),
            }
            for name, run in runs_by_workflow.items()
        ]
        st.dataframe(
            pd.DataFrame(rows), use_container_width=True, hide_index=True,
            column_config={"url": st.column_config.LinkColumn("link", display_text="open")},
        )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

st.title("Options Selector")
st.caption(
    "Read-only, except the Approve & Execute button on the Positions tab, which submits real "
    "(paper) orders once you've checked the review box."
)

tab_overview, tab_positions, tab_decision, tab_performance, tab_ledger, tab_alerts, tab_automation = st.tabs(
    ["Overview", "Positions", "Latest Decision", "Performance", "Ledger Trends", "Alerts", "Automation"]
)

with tab_overview:
    render_overview()
with tab_positions:
    render_positions()
with tab_decision:
    render_latest_decision()
with tab_performance:
    render_performance()
with tab_ledger:
    render_ledger_trends()
with tab_alerts:
    render_alerts()
with tab_automation:
    render_automation()
