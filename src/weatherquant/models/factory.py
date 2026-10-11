"""Fit and freeze the four baselines as loadable artifacts.

The research pipeline fits a residual model, scores it, and throws it away.
The shadow engine has to *reload the exact parameters a decision was made
with*, so this module is the bridge: same pairs, same fitting function, but
written to disk under a version and a cutoff.

Two deliberate differences from the research scorer
---------------------------------------------------
**Fusion is fitted on the fusion.** The research scorer averages ECMWF and GFS
*after* fitting, so its fusion spread is measured against whichever model
built the pairing slot first rather than against the mean it later predicts.
That difference is small in practice (the report's fusion and ECMWF sigmas
differ by ~0.01 degF) but it is a mismatch between what was fitted and what
was predicted, and this is the one place where carrying it forward would be a
choice rather than an oversight. Here the mean *is* the fitted target. See
``research/shadow-validation.md``.

**The cutoff is an argument, not a default.** There is no "use everything"
default because a fit that silently absorbs whatever the database happens to
hold is a fit whose training window nobody chose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from weatherquant.models.artifacts import ModelArtifact, fit_artifact, save_artifact, save_manifest
from weatherquant.models.baselines import load_forecast_observation_pairs

__all__ = ["FIT_SPECS", "FitSpec", "build_pairs", "fit_models"]


@dataclass(frozen=True, slots=True)
class FitSpec:
    """Which source forecasts feed one artifact, and how."""

    sources: tuple[str, ...]
    apply_bias: bool
    fuse: bool


#: Names match the spec and the research report's model column.
FIT_SPECS: dict[str, FitSpec] = {
    "ecmwf_raw": FitSpec(sources=("ecmwf_ifs025",), apply_bias=False, fuse=False),
    "gfs_raw": FitSpec(sources=("gfs_seamless",), apply_bias=False, fuse=False),
    "fusion": FitSpec(sources=("ecmwf_ifs025", "gfs_seamless"), apply_bias=False, fuse=True),
    "calibrated": FitSpec(sources=("ecmwf_ifs025",), apply_bias=True, fuse=False),
}


def build_pairs(
    con,
    *,
    spec: FitSpec,
    observation_rule: str = "max of all readings",
) -> tuple[list[dict], list[str]]:
    """Forecast/observation pairs for one spec, from the research database.

    Returns ``(pairs, problems)``. For a single-source spec that is just that
    source's pairs. For fusion it is the intersection over sources of
    ``(target_date, run_time)``, with the point forecast replaced by the mean -
    a fusion of one model is not a fusion, so partial keys are dropped and
    counted rather than blended.
    """
    loaded: dict[str, list[dict]] = {}
    problems: list[str] = []
    for source in spec.sources:
        rows, source_problems = load_forecast_observation_pairs(con, model=source, observation_rule=observation_rule)
        loaded[source] = rows
        problems.extend(f"{source}: {p}" for p in source_problems)

    if not spec.fuse:
        return list(loaded[spec.sources[0]]), problems

    by_key: dict[tuple, dict] = {}
    for source in spec.sources:
        for pair in loaded.get(source, []):
            key = (pair["target_date"], pair["run_time_utc"])
            slot = by_key.setdefault(key, {"values": [], "base": pair, "models": set()})
            slot["values"].append(pair["predicted_max_f"])
            slot["models"].add(source)

    out: list[dict] = []
    partial = 0
    for key in sorted(by_key, key=lambda k: (k[0], k[1])):
        slot = by_key[key]
        if len(slot["models"]) < len(spec.sources):
            partial += 1
            continue
        merged = dict(slot["base"])
        merged["predicted_max_f"] = sum(slot["values"]) / len(slot["values"])
        out.append(merged)
    if partial:
        problems.append(
            f"fusion dropped {partial} run(s) present in only some of {len(spec.sources)} models"
        )
    if not out:
        problems.append("fusion produced no pairs at all")
    return out, problems


def fit_models(
    con,
    *,
    cutoff: date,
    models_dir: str | Path,
    version: str,
    models: list[str] | None = None,
    observation_rule: str = "max of all readings",
    min_samples_per_bucket: int = 5,
    pin: bool = False,
) -> tuple[list[ModelArtifact], dict[str, list[str]]]:
    """Fit, freeze and (optionally) pin the requested models.

    Every pair after ``cutoff`` is dropped before fitting and the cutoff is
    stamped onto the artifact; :func:`weatherquant.models.artifacts.fit_artifact`
    re-checks that nothing slipped through and hashes the rows it saw.
    """
    names = list(FIT_SPECS) if models is None else models
    unknown = [n for n in names if n not in FIT_SPECS]
    if unknown:
        raise KeyError(f"unknown model(s) {unknown}; known: {sorted(FIT_SPECS)}")

    cutoff_utc = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC)
    artifacts: list[ModelArtifact] = []
    problems: dict[str, list[str]] = {}

    for name in names:
        spec = FIT_SPECS[name]
        rows, row_problems = build_pairs(con, spec=spec, observation_rule=observation_rule)
        kept = [p for p in rows if p.get("target_date") is not None and p["target_date"] <= cutoff]
        dropped = len(rows) - len(kept)
        notes = list(row_problems)
        if dropped:
            notes.append(f"{dropped} pair(s) after the cutoff {cutoff} were excluded")
        if not kept:
            notes.append("no pairs remain after applying the cutoff")

        artifact = fit_artifact(
            model_name=name,
            model_version=version,
            pairs=kept,
            training_cutoff_utc=cutoff_utc,
            apply_bias=spec.apply_bias,
            min_samples_per_bucket=min_samples_per_bucket,
        )
        save_artifact(artifact, models_dir)
        artifacts.append(artifact)
        problems[name] = notes

    if pin:
        save_manifest(models_dir, {a.model_name: a.model_version for a in artifacts})
    return artifacts, problems
