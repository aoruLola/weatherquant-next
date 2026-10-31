"""Time-alignment and mode-isolation audit over the recorded shadow history.

WQN-25 asks for something the runtime gates cannot give on their own: proof,
from the rows that were actually written, that the four shadow models
(``calibrated``, ``ecmwf_raw``, ``fusion``, ``gfs_raw``) were only ever fed
inputs that existed at the instant each decision claims, and that LIVE and
REPLAY never blend into one statistic.

The runtime gates (`ResolvedForecast.usable_at`, the training-cutoff check,
the mode column) refuse bad input *as it arrives*. This module re-derives the
same invariants from ``shadow_decisions`` and ``model_predictions`` after the
fact, which is what makes them auditable: a gate that was bypassed by a bug,
a config change or a hand-edited row shows up here as a named violation with
the offending row attached.

Three families of checks, one per clause of the acceptance criteria:

**No future forecast was read** - for every decision joined to the prediction
it used: the forecast run initialised at or before the decision instant, and
the run was publishable (run + conservative lag) at or before it. The lag is
what "issue_time vs decision timestamp" means here: a run that *exists* is
not a run that was *knowable*.

**Duplication is traceable, not independent** - markets settling the same
(station, target_date) are one underlying weather day priced several times,
and a market whose target date moved across its life is one event spanning
dates. Neither is refused - both are legal market structure - but they are
correlated samples (quant principle 6), so the audit lists them as exposure
groups rather than letting them read as independent opportunities.

**LIVE and REPLAY do not mix** - every row carries a usable mode, every cycle
is one mode end to end, a LIVE row's instant sits next to its write time, and
a REPLAY row's instant never postdates the write that recorded it. With those
holding, any statistic sliced by mode - as the episode key and the win-rate
dimensions already are - is computed over one population.

Everything here is either an aggregate, a set-based anti-join, or a bounded
sample: the audit runs against a live-sized history from an operator's shell
and must not become the thing that stalls it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "ALIGNMENT_MODEL_NAMES",
    "AlignmentReport",
    "AlignmentViolation",
    "ExposureGroup",
    "audit_alignment",
]

#: The four shadow models whose inputs and outputs the audit reconciles.
#: Mirrors ``MODEL_SPECS`` in shadow/forecast.py; declared separately so an
#: unknown model name in the data is a finding, not a crash.
ALIGNMENT_MODEL_NAMES = ("calibrated", "ecmwf_raw", "fusion", "gfs_raw")

#: Which shadow model's recorded output must trace to which same-instant
#: source rows. ``fusion`` carries the real constraint: both sources at the
#: *same* run time, or the average blends two initialisations.
_MODEL_SOURCES: dict[str, tuple[str, ...]] = {
    "calibrated": ("ecmwf_raw",),
    "fusion": ("ecmwf_raw", "gfs_raw"),
}

#: How far a LIVE decision's instant may sit behind the moment its row was
#: written. A live decision is judged at the clock and persisted in the same
#: cycle, so minutes are process jitter; hours mean the row was replayed and
#: labelled live.
_LIVE_DRIFT_LIMIT_SECONDS = 600.0

#: Hard caps on the lists the report carries. The counts stay exact; the
#: samples are bounded so a badly broken history produces a readable report
#: rather than a paste of a million lines.
_MAX_LISTED = 100


@dataclass(frozen=True, slots=True)
class AlignmentViolation:
    """One row (or pair of rows) that breaks an alignment invariant."""

    kind: str
    detail: str
    decision_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "decision_id": self.decision_id}


@dataclass(frozen=True, slots=True)
class ExposureGroup:
    """A set of decisions that are correlated, with the reason spelled out.

    ``kind`` is ``same_weather_day`` (several markets, one station+target
    date) or ``market_date_moved`` (one market quoted under several target
    dates). The groups are capped in the report, not here - this is the raw
    traceability record.
    """

    kind: str
    key: str
    decision_count: int
    accept_count: int
    market_ids: tuple[str, ...]
    event_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "decision_count": self.decision_count,
            "accept_count": self.accept_count,
            "market_ids": list(self.market_ids),
            "event_ids": list(self.event_ids),
        }


@dataclass(slots=True)
class AlignmentReport:
    """Everything the audit found, in a shape a CLI or a test can read."""

    checked_decisions: int = 0
    checked_predictions: int = 0
    unknown_models: list[str] = field(default_factory=list)
    violations: list[AlignmentViolation] = field(default_factory=list)
    violation_counts: dict[str, int] = field(default_factory=dict)
    partial_local_day_decisions: int = 0
    exposure_groups: list[ExposureGroup] = field(default_factory=list)
    forecast_sharing_markets: list[dict[str, Any]] = field(default_factory=list)
    per_mode: dict[str, dict[str, int]] = field(default_factory=dict)
    generated_at_utc: str | None = None

    @property
    def consistent(self) -> bool:
        """True when no invariant broke - duplication may still be listed.

        Exposure groups are structure to be traced, not defects to be fixed;
        counting them as failures would train whoever reads this report to
        ignore a section that always fires on real market data.
        """
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_decisions": self.checked_decisions,
            "checked_predictions": self.checked_predictions,
            "unknown_models": list(self.unknown_models),
            "consistent": self.consistent,
            "violation_count": len(self.violations),
            "violation_counts": dict(self.violation_counts),
            "violations": [v.as_dict() for v in self.violations[:_MAX_LISTED]],
            "partial_local_day_decisions": self.partial_local_day_decisions,
            "exposure_groups": [g.as_dict() for g in self.exposure_groups[:_MAX_LISTED]],
            "forecast_sharing_markets": self.forecast_sharing_markets[:_MAX_LISTED],
            "per_mode": self.per_mode,
            "generated_at_utc": self.generated_at_utc,
        }

    def add(self, violation: AlignmentViolation) -> None:
        self.violations.append(violation)
        self.violation_counts[violation.kind] = self.violation_counts.get(violation.kind, 0) + 1


def audit_alignment(con: Any, *, now: datetime | None = None) -> AlignmentReport:
    """Re-derive the alignment invariants from the recorded history.

    Read-only, over the connection the caller holds. The engine holds the
    write lease on its database for its whole life, so this runs either from
    inside a runner tick or from the offline CLI - never from a second
    process against a live file.
    """
    stamp = (now or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
    report = AlignmentReport(generated_at_utc=stamp)

    _audit_prediction_alignment(con, report)
    _audit_mode_isolation(con, report)
    _audit_model_traceability(con, report)
    _audit_exposure(con, report)
    _summarise_per_mode(con, report)
    return report


# --- no future forecast was read ----------------------------------------------


def _audit_prediction_alignment(con: Any, report: AlignmentReport) -> None:
    """Every decision's prediction existed, and was publishable, at its instant.

    Streamed in pages rather than fetched whole: this is the one pass over
    every model-carrying decision, and it has to stay flat in memory against
    a live-sized history. Only a full prediction key supplies forecast evidence.
    The grouped dates distinguish an absent prediction from a wrong-day one
    without multiplying a decision by the number of available forecasts.
    """
    cursor = con.execute(
        """
        SELECT d.decision_id, d.model_name, d.target_date, d.decision_time_utc,
               d.forecast_run_time_utc, p.rowid, dates.first_target,
               p.forecast_publishable_at_utc, p.n_hours, p.n_missing_hours
        FROM shadow_decisions d
        LEFT JOIN model_predictions p
          USING (model_name, model_version, station, event_id, forecast_run_time_utc, target_date)
        LEFT JOIN (
            SELECT model_name, model_version, station, event_id, forecast_run_time_utc,
                   MIN(target_date) AS first_target
            FROM model_predictions GROUP BY ALL
        ) dates USING (model_name, model_version, station, event_id, forecast_run_time_utc)
        WHERE d.model_name IS NOT NULL
        """
    )
    seen_predictions: set[int] = set()

    while True:
        page = cursor.fetchmany(5000)
        if not page:
            break
        report.checked_decisions += len(page)
        for row in page:
            (
                decision_id,
                model_name,
                decision_target,
                decision_time,
                forecast_run,
                prediction_rowid,
                first_target,
                publishable_at,
                n_hours,
                n_missing_hours,
            ) = row
            if model_name not in ALIGNMENT_MODEL_NAMES and model_name not in report.unknown_models:
                report.unknown_models.append(str(model_name))
            if forecast_run is not None and forecast_run > decision_time:
                report.add(
                    AlignmentViolation(
                        kind="FUTURE_FORECAST_RUN",
                        detail=(
                            f"forecast run {forecast_run.isoformat()} initialised "
                            f"after the decision instant {decision_time.isoformat()}"
                        ),
                        decision_id=str(decision_id),
                    )
                )
            if prediction_rowid is None:
                if decision_target is not None and first_target is not None:
                    kind = "TARGET_DATE_MISMATCH"
                    detail = (
                        f"decision settles {decision_target.isoformat()} but there is no prediction "
                        f"for that date; the first available target is {first_target.isoformat()}"
                    )
                else:
                    kind = "PREDICTION_UNTRACEABLE"
                    detail = f"no recorded prediction matches {model_name}@{forecast_run} for target {decision_target}"
                report.add(AlignmentViolation(kind=kind, detail=detail, decision_id=str(decision_id)))
                continue
            seen_predictions.add(prediction_rowid)
            if publishable_at is not None and publishable_at > decision_time:
                report.add(
                    AlignmentViolation(
                        kind="FORECAST_NOT_PUBLISHABLE",
                        detail=(
                            f"run {forecast_run.isoformat()} became publishable at "
                            f"{publishable_at.isoformat()}, after the decision "
                            f"instant {decision_time.isoformat()}; the decision "
                            "used an issue that was not knowable yet"
                        ),
                        decision_id=str(decision_id),
                    )
                )
            # Predictions are immutable per model/version/run/event/day. A later
            # LIVE cycle or an earlier REPLAY cycle can reuse that distribution;
            # its first evaluation timestamp is not the forecast availability.
            if n_hours is not None and n_missing_hours is not None and n_missing_hours > 0:
                # The local civil day is the unit the market settles and the
                # model was fitted on. A partially covered day remains a legal
                # forecast (the runtime gate allows it), so it is counted, not
                # flagged: an audit that calls legal rows broken is an audit
                # nobody believes when it matters.
                report.partial_local_day_decisions += 1
    report.checked_predictions = len(seen_predictions)


# --- LIVE and REPLAY do not mix -----------------------------------------------


def _audit_mode_isolation(con: Any, report: AlignmentReport) -> None:
    """Rows carry a usable mode; cycles are one mode; instants match the mode.

    The instant checks are SQL-side filters, so only the offending rows cross
    the boundary into Python - on a healthy history that is none at all.
    """
    for decision_id, mode in con.execute(
        "SELECT decision_id, COALESCE(mode, 'NULL') FROM shadow_decisions "
        "WHERE mode IS NULL OR mode = 'UNKNOWN' LIMIT 1000"
    ).fetchall():
        report.add(
            AlignmentViolation(
                kind="MODE_UNUSABLE",
                detail=(
                    f"decision carries mode {mode}; a statistic sliced by mode "
                    "cannot place this row, so LIVE and REPLAY would mix"
                ),
                decision_id=str(decision_id),
            )
        )

    for cycle_id, modes in con.execute(
        "SELECT cycle_id, COUNT(DISTINCT mode) AS modes FROM shadow_decisions "
        "GROUP BY cycle_id HAVING COUNT(DISTINCT mode) > 1 LIMIT 200"
    ).fetchall():
        report.add(
            AlignmentViolation(
                kind="CYCLE_MODE_MIXED",
                detail=(
                    f"cycle {cycle_id} spans {int(modes)} modes; one cycle is one "
                    "instant and must belong to one mode entirely"
                ),
            )
        )

    for decision_id, decision_time, created_at in con.execute(
        "SELECT decision_id, decision_time_utc, created_at_utc FROM shadow_decisions "
        "WHERE mode = 'LIVE' "
        f"  AND epoch(created_at_utc - decision_time_utc) > {_LIVE_DRIFT_LIMIT_SECONDS} "
        "LIMIT 1000"
    ).fetchall():
        lag_minutes = (created_at - decision_time).total_seconds() / 60
        report.add(
            AlignmentViolation(
                kind="LIVE_ROW_REPLAYED",
                detail=(
                    f"LIVE row judged at {decision_time.isoformat()} but written "
                    f"{lag_minutes:.0f} minutes later; a live decision is made "
                    "and persisted in the same cycle"
                ),
                decision_id=str(decision_id),
            )
        )

    for decision_id, decision_time, created_at in con.execute(
        "SELECT decision_id, decision_time_utc, created_at_utc FROM shadow_decisions "
        "WHERE mode = 'REPLAY' AND decision_time_utc > created_at_utc LIMIT 1000"
    ).fetchall():
        report.add(
            AlignmentViolation(
                kind="REPLAY_INSTANT_IN_FUTURE",
                detail=(
                    f"REPLAY row claims archive instant "
                    f"{decision_time.isoformat()} which postdates its own write "
                    f"at {created_at.isoformat()}; the replayed archive cannot "
                    "represent a moment that had not happened"
                ),
                decision_id=str(decision_id),
            )
        )


# --- the four models' inputs line up with their outputs ------------------------


def _audit_model_traceability(con: Any, report: AlignmentReport) -> None:
    """A multi-source output must trace to its sources at the same instant.

    Same-run alignment is enforced when a forecast is resolved (fusion
    refuses rather than blending two initialisations); this checks the
    *recorded* history agrees, with one set-based anti-join per (model,
    source) pair instead of a query per row.
    """
    for model, sources in _MODEL_SOURCES.items():
        for source in sources:
            rows = con.execute(
                "SELECT p.event_id, p.station, p.target_date, p.decision_time_utc, "
                "       p.forecast_run_time_utc "
                "FROM model_predictions p "
                "WHERE p.model_name = ? AND NOT EXISTS ("
                "  SELECT 1 FROM model_predictions s "
                "  WHERE s.model_name = ? "
                "    AND s.station = p.station "
                "    AND s.target_date = p.target_date "
                "    AND s.event_id IS NOT DISTINCT FROM p.event_id "
                "    AND s.decision_time_utc = p.decision_time_utc "
                "    AND s.forecast_run_time_utc = p.forecast_run_time_utc) "
                f"LIMIT {_MAX_LISTED}",
                [model, source],
            ).fetchall()
            for event_id, _station, _target_date, prediction_time, run_time in rows:
                report.add(
                    AlignmentViolation(
                        kind="MODEL_INPUT_UNTRACEABLE",
                        detail=(
                            f"{model} output for {event_id} at "
                            f"{prediction_time.isoformat()} (run "
                            f"{run_time.isoformat()}) has no same-instant "
                            f"{source} row to trace to"
                        )
                        + ("; fusion must average one initialisation, not two" if model == "fusion" else ""),
                    )
                )


# --- duplication is traceable --------------------------------------------------


def _audit_exposure(con: Any, report: AlignmentReport) -> None:
    """List the correlation structure instead of letting it read as independence."""
    same_day = con.execute(
        "SELECT station, target_date, COUNT(*) AS decisions, "
        "       SUM(CASE WHEN state = 'ACCEPT' THEN 1 ELSE 0 END) AS accepts, "
        "       COUNT(DISTINCT market_id) AS markets "
        "FROM shadow_decisions WHERE target_date IS NOT NULL "
        "GROUP BY station, target_date HAVING COUNT(DISTINCT market_id) > 1 "
        f"ORDER BY markets DESC LIMIT {_MAX_LISTED}"
    ).fetchall()
    for station, target_date, decisions, accepts, _markets in same_day:
        ids = con.execute(
            "SELECT DISTINCT market_id, event_id FROM shadow_decisions WHERE station = ? AND target_date = ?",
            [station, target_date],
        ).fetchall()
        report.exposure_groups.append(
            ExposureGroup(
                kind="same_weather_day",
                key=f"{station}/{target_date.isoformat()}",
                decision_count=int(decisions),
                accept_count=int(accepts or 0),
                market_ids=tuple(sorted(str(m) for m, _e in ids)),
                event_ids=tuple(sorted({str(e) for _m, e in ids if e})),
            )
        )

    moved = con.execute(
        "SELECT market_id, COUNT(DISTINCT target_date) AS dates, COUNT(*) AS decisions "
        "FROM shadow_decisions WHERE target_date IS NOT NULL "
        f"GROUP BY market_id HAVING COUNT(DISTINCT target_date) > 1 LIMIT {_MAX_LISTED}"
    ).fetchall()
    for market_id, _dates, decisions in moved:
        targets = con.execute(
            "SELECT DISTINCT target_date, event_id FROM shadow_decisions WHERE market_id = ?",
            [market_id],
        ).fetchall()
        report.exposure_groups.append(
            ExposureGroup(
                kind="market_date_moved",
                key=str(market_id),
                decision_count=int(decisions),
                accept_count=0,
                market_ids=(str(market_id),),
                event_ids=tuple(sorted({str(e) for _t, e in targets if e})),
            )
        )

    # One forecast run feeding many markets: correlated samples by construction
    # (principle 6). Reported per run so an "N opportunities" headline can be
    # read against how many independent weather inputs actually existed.
    sharing = con.execute(
        "SELECT model_name, forecast_run_time_utc, COUNT(DISTINCT market_id) AS markets, "
        "       COUNT(*) AS decisions FROM shadow_decisions "
        "WHERE model_name IS NOT NULL AND forecast_run_time_utc IS NOT NULL "
        "GROUP BY model_name, forecast_run_time_utc "
        f"HAVING COUNT(DISTINCT market_id) > 1 ORDER BY decisions DESC LIMIT {_MAX_LISTED}"
    ).fetchall()
    for model, run_time, markets, decisions in sharing:
        report.forecast_sharing_markets.append(
            {
                "model_name": str(model),
                "forecast_run_time_utc": run_time.isoformat(),
                "markets": int(markets),
                "decisions": int(decisions),
            }
        )


def _summarise_per_mode(con: Any, report: AlignmentReport) -> None:
    """The counts any statistic must be sliceable by, so the split is visible."""
    rows = con.execute(
        "SELECT COALESCE(mode, 'UNKNOWN'), COUNT(*), "
        "       SUM(CASE WHEN state = 'ACCEPT' THEN 1 ELSE 0 END), "
        "       COUNT(DISTINCT market_id) "
        "FROM shadow_decisions GROUP BY 1"
    ).fetchall()
    for mode, decisions, accepts, markets in rows:
        report.per_mode[str(mode)] = {
            "decisions": int(decisions),
            "accepts": int(accepts or 0),
            "distinct_markets": int(markets or 0),
        }
