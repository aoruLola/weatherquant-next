"""Versioned model artifacts: immutable, cutoff-stamped, pinned.

The properties under test are the ones that make "we decided with model X
version Y fitted on data up to Z" a checkable statement rather than a
plausible-sounding label.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from weatherquant.models.artifacts import (
    ARTIFACT_SPEC_VERSION,
    artifact_path,
    fit_artifact,
    hash_pairs,
    load_artifact,
    load_manifest,
    save_artifact,
    save_manifest,
)

CUTOFF = datetime(2026, 9, 20, tzinfo=UTC)


def _pairs(n: int = 12, *, last_date: date | None = None) -> list[dict]:
    """Pairs spread over consecutive dates, deterministic by construction."""
    last = last_date or CUTOFF.date()
    rows = []
    for i in range(n):
        day = last - timedelta(days=i % 6)
        rows.append(
            {
                "target_date": day,
                "run_time_utc": datetime(2026, 9, 1, 0, tzinfo=UTC) + timedelta(hours=6 * i),
                "lead_hours": 12.0 + (i % 5) * 24,
                "predicted_max_f": 60.0 + (i % 4),
                "observed_max_f": 61.0 + (i % 4),
            }
        )
    return rows


# --- fitting ---------------------------------------------------------------


def test_a_fit_carries_its_cutoff_and_hashes_the_rows_it_saw() -> None:
    pairs = _pairs()
    artifact = fit_artifact(
        model_name="calibrated",
        model_version="v1",
        pairs=pairs,
        training_cutoff_utc=CUTOFF,
        apply_bias=True,
    )
    assert artifact.training_cutoff_utc == CUTOFF
    assert artifact.source_data_hash == hash_pairs(pairs)
    assert artifact.n_pairs == len(pairs)
    assert artifact.sigma_by_lead  # something was actually fitted
    assert artifact.spec_version == ARTIFACT_SPEC_VERSION


def test_a_fit_refuses_rows_after_the_cutoff_rather_than_silently_including_them() -> None:
    """Filtering is the caller's job; a mislabeled cutoff must not be absorbable."""
    pairs = [
        *_pairs(),
        {
            "target_date": date(2026, 12, 1),
            "run_time_utc": datetime(2026, 12, 1, tzinfo=UTC),
            "lead_hours": 12.0,
            "predicted_max_f": 60.0,
            "observed_max_f": 62.0,
        },
    ]
    with pytest.raises(ValueError, match=r"after the training cutoff"):
        fit_artifact(
            model_name="calibrated",
            model_version="v1",
            pairs=pairs,
            training_cutoff_utc=CUTOFF,
            apply_bias=True,
        )


def test_raw_models_pin_the_bias_to_zero_so_the_ablation_is_real() -> None:
    raw = fit_artifact(
        model_name="ecmwf_raw",
        model_version="v1",
        pairs=_pairs(),
        training_cutoff_utc=CUTOFF,
        apply_bias=False,
    )
    calibrated = fit_artifact(
        model_name="calibrated",
        model_version="v1",
        pairs=_pairs(),
        training_cutoff_utc=CUTOFF,
        apply_bias=True,
    )
    assert raw.bias_f == 0.0
    # Same rows, same spread - only the location differs.
    assert raw.sigma_by_lead == calibrated.sigma_by_lead
    assert calibrated.bias_f != 0.0


def test_the_same_rows_always_hash_the_same() -> None:
    pairs = _pairs()
    assert hash_pairs(pairs) == hash_pairs(list(reversed(pairs)))
    assert hash_pairs(pairs) != hash_pairs(pairs[:-1])


# --- immutability ----------------------------------------------------------


def test_saving_over_an_existing_version_is_refused(tmp_path) -> None:
    artifact = fit_artifact(
        model_name="calibrated",
        model_version="v1",
        pairs=_pairs(),
        training_cutoff_utc=CUTOFF,
        apply_bias=True,
    )
    save_artifact(artifact, tmp_path)
    with pytest.raises(FileExistsError, match="immutable"):
        save_artifact(artifact, tmp_path)


def test_a_round_trip_preserves_every_field(tmp_path) -> None:
    artifact = fit_artifact(
        model_name="fusion",
        model_version="v2",
        pairs=_pairs(),
        training_cutoff_utc=CUTOFF,
        apply_bias=False,
    )
    save_artifact(artifact, tmp_path)
    loaded = load_artifact(tmp_path, "fusion", "v2")

    assert loaded == artifact
    assert loaded.residual.sigma_by_lead == artifact.residual.sigma_by_lead
    assert loaded.residual.is_fitted


def test_loading_a_name_that_disagrees_with_the_file_is_refused(tmp_path) -> None:
    """The file's own declaration wins: a renamed artifact is not the artifact.

    Reachable when a models directory is copied or hand-edited, which is exactly
    when "I asked for gfs_raw and got calibrated" must be caught rather than
    silently used as the other model.
    """
    artifact = fit_artifact(
        model_name="calibrated",
        model_version="v1",
        pairs=_pairs(),
        training_cutoff_utc=CUTOFF,
        apply_bias=True,
    )
    save_artifact(artifact, tmp_path)
    import shutil

    target_dir = tmp_path / "gfs_raw"
    target_dir.mkdir(exist_ok=True)
    shutil.copy(artifact_path(tmp_path, "calibrated", "v1"), target_dir / "v1.json")

    with pytest.raises(ValueError, match="declares"):
        load_artifact(tmp_path, "gfs_raw", "v1")


def test_an_unsupported_spec_version_is_refused_not_guessed(tmp_path) -> None:
    artifact = fit_artifact(
        model_name="calibrated",
        model_version="v1",
        pairs=_pairs(),
        training_cutoff_utc=CUTOFF,
        apply_bias=True,
    )
    path = save_artifact(artifact, tmp_path)
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["spec_version"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="spec_version"):
        load_artifact(tmp_path, "calibrated", "v1")


def test_an_artifact_missing_its_cutoff_is_refused(tmp_path) -> None:
    import json

    artifact = fit_artifact(
        model_name="calibrated",
        model_version="v1",
        pairs=_pairs(),
        training_cutoff_utc=CUTOFF,
        apply_bias=True,
    )
    path = save_artifact(artifact, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["training_cutoff_utc"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="missing required field"):
        load_artifact(tmp_path, "calibrated", "v1")


def test_a_missing_artifact_is_a_named_file_error(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="model artifact not found"):
        load_artifact(tmp_path, "calibrated", "nope")


# --- usability -------------------------------------------------------------


def test_an_artifact_fitted_after_the_moment_in_use_is_refused() -> None:
    artifact = fit_artifact(
        model_name="calibrated",
        model_version="v1",
        pairs=_pairs(),
        training_cutoff_utc=CUTOFF,
        apply_bias=True,
    )
    ok, why = artifact.usable_at(CUTOFF + timedelta(days=1))
    assert ok is True and why is None

    refused, why = artifact.usable_at(CUTOFF - timedelta(days=1))
    assert refused is False
    assert "after the decision instant" in why


def test_an_artifact_with_no_spread_at_all_is_unusable(tmp_path) -> None:
    import json

    artifact = fit_artifact(
        model_name="calibrated",
        model_version="v1",
        pairs=[],  # nothing to fit: no sigmas
        training_cutoff_utc=CUTOFF,
        apply_bias=True,
    )
    ok, _why = artifact.usable_at(CUTOFF + timedelta(hours=1))
    assert ok is False

    path = save_artifact(artifact, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sigma_by_lead"] = {}
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_artifact(tmp_path, "calibrated", "v1")
    refused, why = loaded.usable_at(CUTOFF + timedelta(hours=1))
    assert refused is False
    assert "no usable spread" in why


def test_a_sigma_thinner_than_the_minimum_does_not_leak_a_fallback() -> None:
    """A two-sample bucket has no spread; the model says so instead of borrowing."""
    from weatherquant.models.residual import fit_residual_model

    pairs = [
        {
            "target_date": date(2026, 9, 19),
            "lead_hours": 12.0,
            "predicted_max_f": 60.0,
            "observed_max_f": 61.0,
        },
        {
            "target_date": date(2026, 9, 19),
            "lead_hours": 13.0,
            "predicted_max_f": 60.0,
            "observed_max_f": 62.0,
        },
    ]
    model = fit_residual_model(pairs, model_id="x", min_samples_per_bucket=5)
    assert model.sigma(12.0) is None


# --- manifest --------------------------------------------------------------


def test_the_manifest_pins_versions_and_only_existing_ones(tmp_path) -> None:
    for name in ("ecmwf_raw", "gfs_raw"):
        artifact = fit_artifact(
            model_name=name,
            model_version="v1",
            pairs=_pairs(),
            training_cutoff_utc=CUTOFF,
            apply_bias=False,
        )
        save_artifact(artifact, tmp_path)

    save_manifest(tmp_path, {"ecmwf_raw": "v1", "gfs_raw": "v1"})
    assert load_manifest(tmp_path) == {"ecmwf_raw": "v1", "gfs_raw": "v1"}


def test_pin_refuses_a_version_that_is_not_on_disk(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="no such artifact"):
        save_manifest(tmp_path, {"calibrated": "v1"})


def test_a_missing_manifest_is_an_error_not_an_empty_pin(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="cannot choose a version"):
        load_manifest(tmp_path)


def test_a_manifest_with_an_unsupported_spec_version_is_refused(tmp_path) -> None:
    import json

    (tmp_path / "manifest.json").write_text(
        json.dumps({"spec_version": 99, "pinned": {}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="spec_version"):
        load_manifest(tmp_path)


def test_artifact_paths_are_namespaced_by_model_and_version(tmp_path) -> None:
    path = artifact_path(tmp_path, "calibrated", "v7")
    assert path == tmp_path / "calibrated" / "v7.json"

