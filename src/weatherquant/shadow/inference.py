"""Turning a resolved forecast into a bracket distribution, under a pinned model.

Three refusals carried over from :mod:`weatherquant.models.probability`, each
surfaced as a decision reason rather than swallowed:

* no usable sigma for this lead time -> ``NO_VALID_MODEL``;
* brackets that do not close at both ends -> ``DATA_QUALITY_FAILURE``;
* an artifact whose training cutoff postdates the decision -> refused, because
  that would be parameters fitted on the answer.

Everything else is memoised on ``(model, version, run, target_date, event)``:
the distribution is a pure function of those, so recomputing it per market per
cycle would burn CPU to reproduce identical bytes. The memo key deliberately
includes the *event*, because two events can settle on the same date with
different ladders, and a cache keyed only on the date would serve one ladder's
probabilities to the other.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from weatherquant.markets.models import Bracket, BracketDistribution
from weatherquant.models.artifacts import ModelArtifact, load_artifact, load_manifest
from weatherquant.models.probability import distribution_for_event
from weatherquant.shadow.forecast import ResolvedForecast
from weatherquant.shadow.state import MarketGroup

__all__ = ["ModelRefusal", "PredictionResult", "Predictor"]


@dataclass(frozen=True, slots=True)
class ModelRefusal:
    """Why no distribution could be produced. Never a bare "error"."""

    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class PredictionResult:
    shadow_model: str
    model_version: str
    training_cutoff_utc: datetime
    source_data_hash: str
    model_run_id: str
    distribution: BracketDistribution
    predicted_max_f: float
    lead_hours: float
    from_cache: bool

    @property
    def probabilities(self) -> dict[str, float]:
        return self.distribution.as_dict()


class Predictor:
    """Loads pinned artifacts once, then serves (or refuses) predictions."""

    def __init__(
        self,
        models_dir: str,
        *,
        model_names: list[str],
        clock=None,
    ) -> None:
        self.models_dir = models_dir
        self.model_names = list(model_names)
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.artifacts: dict[str, ModelArtifact] = {}
        self.pinned_versions: dict[str, str] = {}
        self._memo: dict[tuple, PredictionResult] = {}

    # -- loading ------------------------------------------------------------

    def load(self) -> list[str]:
        """Load the manifest's pins. Raises if any named model is unpinned."""
        pinned = load_manifest(self.models_dir)
        missing = [name for name in self.model_names if name not in pinned]
        if missing:
            raise FileNotFoundError(
                f"models/manifest.json does not pin {missing}; every model the shadow "
                f"runs must be pinned explicitly, available: {sorted(pinned)}"
            )
        self.pinned_versions = {name: pinned[name] for name in self.model_names}
        self.artifacts = {
            name: load_artifact(self.models_dir, name, pinned[name]) for name in self.model_names
        }
        return [f"{n}@{v}" for n, v in self.pinned_versions.items()]

    @property
    def is_loaded(self) -> bool:
        return bool(self.artifacts)

    def version_rows(self, *, loaded_at_utc: datetime | None = None, primary: str) -> list[tuple]:
        """Rows for ``model_versions``: what was loaded, with content hashes."""
        when = loaded_at_utc or self.clock()
        rows: list[tuple] = []
        for name in self.model_names:
            artifact = self.artifacts[name]
            rows.append(
                (
                    name,
                    artifact.model_version,
                    artifact.training_cutoff_utc,
                    artifact.created_at_utc,
                    artifact.source_data_hash,
                    _sha256_of_artifact(artifact),
                    artifact.n_pairs,
                    artifact.n_dates,
                    when,
                    name == primary,
                )
            )
        return rows

    # -- predicting ---------------------------------------------------------

    def predict(
        self,
        *,
        shadow_model: str,
        forecast: ResolvedForecast,
        group: MarketGroup,
        decision_time: datetime,
    ) -> PredictionResult | ModelRefusal:
        artifact = self.artifacts.get(shadow_model)
        if artifact is None:
            return ModelRefusal(
                "NO_VALID_MODEL",
                f"model {shadow_model!r} is not loaded (loaded: {sorted(self.artifacts)})",
            )

        usable, why = artifact.usable_at(decision_time)
        if not usable:
            return ModelRefusal("NO_VALID_MODEL", why or "artifact refused")

        brackets = tuple(m.bracket for m in group.markets if m.bracket is not None)
        if len(brackets) != len(group.markets):
            return ModelRefusal(
                "DATA_QUALITY_FAILURE",
                f"event {group.event_id} has "
                f"{len(group.markets) - len(brackets)} market(s) with no parseable bracket",
            )

        key = (
            shadow_model,
            artifact.model_version,
            forecast.run_time_utc,
            group.target_date,
            group.event_id,
        )
        cached = self._memo.get(key)
        if cached is not None:
            # Same bytes for the same inputs, so the row is the same row - but
            # recorded as reused, so a report can tell computed from served.
            return dataclasses.replace(cached, from_cache=True)

        try:
            distribution = _build(
                artifact=artifact,
                brackets=brackets,
                forecast=forecast,
                group=group,
            )
        except ValueError as exc:
            return ModelRefusal("NO_VALID_MODEL", str(exc))

        result = PredictionResult(
            shadow_model=shadow_model,
            model_version=artifact.model_version,
            training_cutoff_utc=artifact.training_cutoff_utc,
            source_data_hash=artifact.source_data_hash,
            model_run_id=_model_run_id(shadow_model, artifact, forecast, group),
            distribution=distribution,
            predicted_max_f=forecast.predicted_max_f,
            lead_hours=forecast.lead_hours,
            from_cache=False,
        )
        self._memo[key] = result
        return result

    def clear_memo(self) -> None:
        self._memo.clear()


def _build(
    *,
    artifact: ModelArtifact,
    brackets: tuple[Bracket, ...],
    forecast: ResolvedForecast,
    group: MarketGroup,
) -> BracketDistribution:
    return distribution_for_event(
        forecast.predicted_max_f,
        brackets,
        model=artifact.residual,
        lead_hours=forecast.lead_hours,
        station=group.markets[0].station,
        target_date=group.target_date,
        model_id=artifact.model_name,
        forecast_run_time_utc=forecast.run_time_utc,
    )


def _model_run_id(
    shadow_model: str,
    artifact: ModelArtifact,
    forecast: ResolvedForecast,
    group: MarketGroup,
) -> str:
    """Deterministic id: same inputs, same id, across restarts.

    A random id would make "was this prediction recomputed or reused?" a
    question only the memo could answer, and the memo does not survive a
    restart.
    """
    basis = "|".join(
        [
            shadow_model,
            artifact.model_version,
            forecast.run_time_utc.isoformat(),
            group.target_date.isoformat(),
            group.event_id,
            artifact.source_data_hash[:16],
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _sha256_of_artifact(artifact: ModelArtifact) -> str:
    blob = json.dumps(artifact.to_json_dict(), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def prediction_rows(
    *,
    result: PredictionResult,
    forecast: ResolvedForecast,
    group: MarketGroup,
    station: str,
    decision_time: datetime,
    computed_at: datetime,
) -> tuple:
    """A row for ``model_predictions`` carrying every field the spec requires."""
    return (
        result.shadow_model,
        result.model_version,
        station,
        group.target_date,
        group.event_id,
        forecast.run_time_utc,
        json.dumps(result.probabilities, sort_keys=True),
        result.predicted_max_f,
        result.lead_hours,
        forecast.n_hours,
        forecast.n_missing_hours,
        result.model_run_id,
        result.training_cutoff_utc,
        forecast.publishable_at_utc,
        forecast.publishable_basis,
        forecast.received_at_utc,
        decision_time,
        result.model_version,  # calibration_version: bias and spread ship in one artifact
        result.source_data_hash,
        computed_at,
    )
