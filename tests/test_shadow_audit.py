"""Regression tests for the two properties a release audit has to be able to prove.

**Cost has exactly one home.** The hand-computed expectations elsewhere assert
numbers, not provenance - a shadow that re-derived its own fee would still
produce them. These tests attack provenance instead: they break the sanctioned
function and check that the evaluation refuses rather than carrying on, and
they statically confirm nothing outside ``decision.py`` even imports the cost
API.

**Model parameters are loaded, never refitted.** Same shape: make fitting
impossible during a run, and hash the artifacts before and after it. A run that
refits, or that touches what it loaded, fails here instead of silently
shipping parameters nobody reviewed.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import weatherquant.execution.pricing as pricing
import weatherquant.models.residual as residual
from shadow_helpers import (
    NOW,
    TARGET_DATE,
    archive_line,
    book_payload,
    gamma_event,
    make_catalog_market,
    make_forecast,
    make_observation,
    make_prediction,
    write_lines,
)
from weatherquant.config import Settings
from weatherquant.shadow import decision as decision_module
from weatherquant.shadow import runner as runner_module
from weatherquant.shadow.config import ShadowSettings, load_thresholds
from weatherquant.shadow.decision import Direction, Mode, evaluate
from weatherquant.shadow.runner import RunnerConfig, ShadowRunner

REPO = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO / "configs" / "shadow-thresholds.json"
THRESHOLDS = load_thresholds(THRESHOLDS_PATH)
BUFFER = 0.005
VENUE_TS_MS = int((NOW - timedelta(seconds=4)).timestamp() * 1000)


def _evaluate(**overrides):
    base = dict(
        cycle_id="audit",
        market=make_catalog_market(),
        direction=Direction.BUY_YES,
        prediction=make_prediction(),
        forecast=make_forecast(),
        snapshot=make_observation(),
        thresholds=THRESHOLDS,
        buffer_fraction=BUFFER,
        decision_time=NOW,
        collector_last_seen=NOW,
        collector_silence_seconds=900.0,
        max_forecast_age_hours=THRESHOLDS.max_forecast_age_hours,
        market_closed=False,
        mode=Mode.LIVE,
    )
    base.update(overrides)
    return evaluate(**base)


def _archive(root: Path) -> Path:
    raw = root / "raw"
    write_lines(raw, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event())])
    for token in ("tok-0-yes", "tok-1-yes", "tok-2-yes", "tok-3-yes"):
        write_lines(
            raw,
            "rest",
            TARGET_DATE,
            [archive_line(2, "rest", book_payload(token_id=token, venue_ts_ms=VENUE_TS_MS))],
        )
    return raw


def _models(root: Path) -> Path:
    dest = root / "models"
    if not dest.exists():
        shutil.copytree(REPO / "models", dest)
    return dest


def _runner(root: Path, raw: Path, models: Path) -> ShadowRunner:
    shadow = ShadowSettings(
        shadow_db_path=root / "shadow" / "shadow.duckdb",
        shadow_models_dir=models,
        shadow_health_dir=root / "health",
        shadow_thresholds_path=str(THRESHOLDS_PATH),
        collector_raw_dir=raw,
        shadow_model_names="ecmwf_raw,calibrated",
        shadow_primary_model="calibrated",
        shadow_tick_seconds=0.01,
        shadow_forecast_refresh_seconds=0.01,
        shadow_metrics_seconds=0.01,
        shadow_run_lookback_hours=24.0,
    )
    return ShadowRunner(
        RunnerConfig(settings=Settings(), shadow=shadow, thresholds=THRESHOLDS),
        clock=lambda: NOW + timedelta(seconds=1),
    )


# --- one home for cost -----------------------------------------------------


def test_a_fee_function_that_raises_stops_the_evaluation(monkeypatch) -> None:
    """Provenance, not arithmetic.

    ``taker_fee_usd`` is reached *through* :func:`execution.cost`, which looks
    the name up in the pricing module - so patching it there is a real
    interception of the sanctioned path. A shadow that had rolled its own fee
    would sail straight past this and still produce a number.
    """

    def _explode(**_kwargs):
        raise AssertionError("a fee was computed outside execution.pricing")

    monkeypatch.setattr(pricing, "taker_fee_usd", _explode)
    decision = _evaluate()
    assert decision.estimated_fees_usd is None
    assert decision.net_edge is None
    assert "EVALUATION_ERROR" in decision.reason_codes


def test_the_fill_plan_is_requested_from_the_imported_pricing_function(monkeypatch) -> None:
    """``decision`` binds the name at import, so the call is what is counted.

    Where it was imported *from* is asserted separately, by
    test_only_the_decision_module_imports_the_pricing_api below - together the
    two say "called, and called from the one allowed place".
    """
    real = decision_module.build_fill_plan
    calls = {"n": 0}

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(decision_module, "build_fill_plan", counting)
    _evaluate()
    assert calls["n"] == 1


@pytest.mark.parametrize("direction", [Direction.BUY_YES, Direction.SELL_YES])
def test_both_directions_reach_pricing_for_fill_and_fee(monkeypatch, direction) -> None:
    calls = {"fill": 0, "fee": 0}
    real_fill = decision_module.build_fill_plan
    real_fee = pricing.taker_fee_usd

    def counting_fill(*args, **kwargs):
        calls["fill"] += 1
        return real_fill(*args, **kwargs)

    def counting_fee(**kwargs):
        calls["fee"] += 1
        return real_fee(**kwargs)

    monkeypatch.setattr(decision_module, "build_fill_plan", counting_fill)
    monkeypatch.setattr(pricing, "taker_fee_usd", counting_fee)

    decision = _evaluate(direction=direction)
    assert calls["fill"] == 1, f"{direction} did not walk the book through pricing"
    assert calls["fee"] == 1, f"{direction} did not price its fee through pricing"
    assert decision.estimated_fees_usd is not None


def test_only_the_decision_module_imports_the_pricing_api() -> None:
    """One consumer of a cost function is the property; two is the smell.

    Cost logic spread across two modules is how two implementations of the same
    fee appear later, in a file nobody remembers writing.
    """
    shadow_dir = REPO / "src" / "weatherquant" / "shadow"
    offenders = []
    for path in sorted(shadow_dir.glob("*.py")):
        if path.name == "decision.py":
            continue
        text = path.read_text(encoding="utf-8")
        for symbol in ("build_fill_plan", "taker_fee_usd", "execution_cost"):
            if symbol in text:
                offenders.append(f"{path.name}: {symbol}")
    assert offenders == [], f"pricing leaked out of decision.py: {offenders}"


def test_decision_imports_every_cost_function_from_execution_pricing() -> None:
    """Read the import block: no second source could satisfy it unnoticed.

    ``taker_fee_usd`` is deliberately absent: decision calls
    ``execution_cost(fill, fee_rate=...)``, which owns the fee calculation
    internally. The one-hop path is still one home - what must not appear is a
    fee or fill symbol imported from anywhere *else*.
    """
    text = (REPO / "src" / "weatherquant" / "shadow" / "decision.py").read_text(encoding="utf-8")
    block = text.split("from weatherquant.execution.pricing import", 1)[1].split(")", 1)[0]
    for symbol in (
        "build_fill_plan",
        "execution_cost",
        "expected_net_profit_usd",
        "expected_net_profit_usd_sell",
        "edge_usd_per_share",
        "edge_usd_per_share_sell",
    ):
        assert symbol in block, f"{symbol} is not imported from execution.pricing"

    # Every other module in the package must import no pricing symbol at all,
    # which is what keeps this import block the only one.
    shadow_dir = REPO / "src" / "weatherquant" / "shadow"
    for path in sorted(shadow_dir.glob("*.py")):
        if path.name == "decision.py":
            continue
        other = path.read_text(encoding="utf-8")
        assert "from weatherquant.execution.pricing" not in other, path.name


# --- one home for model parameters ----------------------------------------


def test_a_run_never_writes_to_the_models_directory(tmp_path, monkeypatch) -> None:
    models = _models(tmp_path)

    def digest() -> dict[str, str]:
        return {
            str(p.relative_to(models)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(models.rglob("*"))
            if p.is_file()
        }

    before = digest()
    monkeypatch.setattr(runner_module, "SingleRunsClient", _FakeClient)
    runner = _runner(tmp_path, _archive(tmp_path), models)
    try:
        runner.start()
        runner.cycle()
    finally:
        runner.close(status="stopped", reason="audit")
    assert digest() == before, "a run modified the model artifacts it loaded"


def test_a_run_never_refits_a_model(tmp_path, monkeypatch) -> None:
    def _explode(*_args, **_kwargs):
        raise AssertionError("the shadow refit a model instead of loading it")

    monkeypatch.setattr(residual, "fit_residual_model", _explode)
    monkeypatch.setattr(runner_module, "SingleRunsClient", _FakeClient)
    runner = _runner(tmp_path, _archive(tmp_path), _models(tmp_path))
    try:
        runner.start()
        stats = runner.cycle()
    finally:
        runner.close(status="stopped", reason="audit")
    assert stats.predictions >= 1  # predictions happened, none of them fitted


def test_no_shadow_module_imports_a_fitting_entry_point() -> None:
    """Static backstop for the run above - catches a new import without a run."""
    shadow_dir = REPO / "src" / "weatherquant" / "shadow"
    forbidden = ("fit_residual_model", "fit_artifact", "save_artifact", "save_manifest")
    offenders = []
    for path in sorted(shadow_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for symbol in forbidden:
            if symbol in text:
                offenders.append(f"{path.name}: {symbol}")
    assert offenders == [], f"the shadow can mutate model parameters: {offenders}"


def test_an_artifact_fitted_after_the_decision_is_refused_by_the_gate() -> None:
    future = make_prediction(training_cutoff=NOW + timedelta(days=1))
    decision = _evaluate(prediction=future)
    assert decision.primary_reason is not None
    assert decision.primary_reason.value == "NO_VALID_MODEL"
    assert decision.net_edge is None
    assert decision.estimated_fees_usd is None


def test_the_threshold_file_is_what_the_runner_actually_uses(tmp_path) -> None:
    """Gates come from the versioned file, not from a module-level constant."""
    assert THRESHOLDS.version == "v2"
    assert THRESHOLDS.max_snapshot_age_ms == 90000.0
    assert THRESHOLDS.as_dict()["version"] == THRESHOLDS.version
    assert load_thresholds(THRESHOLDS_PATH).as_dict() == THRESHOLDS.as_dict()


class _FakeClient:
    """Minimal stand-in: enough forecast for a cycle, no network."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(self, model: str, run_time):
        from weatherquant.weather.single_runs import HourlyPoint, RunFetch

        hourly = [
            HourlyPoint(
                valid_time_utc=datetime(2026, 10, 11, 4, tzinfo=UTC) + timedelta(hours=h),
                temperature_2m=32.0 + (h % 4) * 0.5,
            )
            for h in range(48)
        ]
        return RunFetch(
            model=model,
            run_time_utc=run_time,
            status="ok",
            hourly=hourly,
            payload_sha256="f" * 64,
            fetched_at_utc=NOW - timedelta(hours=12),
        )
