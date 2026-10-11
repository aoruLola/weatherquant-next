"""Versioned, immutable artifacts for a fitted residual model.

Why this exists
---------------
:func:`weatherquant.models.residual.fit_residual_model` fits in memory and
returns a dataclass. The research pipeline fits, scores and discards; the
shadow engine has to *reload the exact parameters a decision was made with*,
weeks later, without refitting. Refitting at decision time would silently
change every probability, and refitting on data that includes the decision's
own target day would be future leakage wearing a lab coat.

So a fit is exported once, to a file, and never rewritten:

    models/<name>/<version>.json

Three properties are enforced on the way in and out:

* **A version is immutable.** Writing over an existing version is refused.
  A version whose parameters can change is not a version.
* **The cutoff travels with the parameters.** ``training_cutoff_utc`` is the
  last instant whose data the fit saw. Loading an artifact whose cutoff is in
  the future relative to a decision is refused - that is the leak this file
  exists to prevent.
* **The source data is hashed.** ``source_data_hash`` identifies the exact
  rows the fit consumed, so "same version" can be checked rather than assumed.

``models/manifest.json`` pins which version of each model is in use. Pinning
is a separate file from the parameters so that promoting a version is a reviewable
one-line diff, not a rewrite of numbers.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from weatherquant.models.residual import ResidualModel

__all__ = [
    "ARTIFACT_SPEC_VERSION",
    "MANIFEST_NAME",
    "ModelArtifact",
    "artifact_path",
    "fit_artifact",
    "hash_pairs",
    "load_artifact",
    "load_manifest",
    "save_artifact",
    "save_manifest",
]

ARTIFACT_SPEC_VERSION = 1
MANIFEST_NAME = "manifest.json"


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite an existing artifact: {path}")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _field(pair: dict, key: str) -> str:
    value = pair.get(key)
    if value is None:
        return "<none>"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def hash_pairs(pairs: list[dict]) -> str:
    """SHA-256 over the fit inputs, independent of dict ordering.

    Every pair contributes its identity and its numbers; a change to any of
    them changes the hash, so two artifacts claiming the same source hash saw
    the same rows.
    """
    hasher = hashlib.sha256()
    for pair in sorted(pairs, key=lambda p: _field(p, "target_date") + "|" + _field(p, "run_time_utc")):
        line = "|".join(
            _field(pair, key)
            for key in ("target_date", "run_time_utc", "predicted_max_f", "observed_max_f", "lead_hours")
        )
        hasher.update(line.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """One immutable fit, with everything needed to audit or refuse it."""

    model_name: str
    model_version: str
    training_cutoff_utc: datetime
    created_at_utc: datetime
    source_data_hash: str
    spec_version: int
    bias_f: float
    sigma_by_lead: dict[str, float]
    counts_by_lead: dict[str, int]
    n_pairs: int
    n_dates: int
    train_start: date | None
    train_end: date | None
    min_samples_per_bucket: int
    fit_notes: tuple[str, ...]

    @property
    def residual(self) -> ResidualModel:
        """Rebuild the runtime model this artifact was exported from."""
        return ResidualModel(
            model_id=self.model_name,
            bias_f=self.bias_f,
            sigma_by_lead=dict(self.sigma_by_lead),
            counts_by_lead=dict(self.counts_by_lead),
            n_pairs=self.n_pairs,
            n_dates=self.n_dates,
            train_start=self.train_start,
            train_end=self.train_end,
            min_samples_per_bucket=self.min_samples_per_bucket,
            fit_notes=tuple(self.fit_notes),
        )

    def usable_at(self, instant_utc: datetime) -> tuple[bool, str | None]:
        """Refuse an artifact that could not have been fitted by ``instant_utc``.

        The normal case passes. The check exists because "loaded a model trained
        on this afternoon's observations to decide this morning's market" is
        exactly the bug that no exception would otherwise raise.
        """
        if self.training_cutoff_utc > instant_utc:
            return False, (
                f"model {self.model_name}@{self.model_version} was fitted on data up to "
                f"{self.training_cutoff_utc.isoformat()}, after the decision instant "
                f"{instant_utc.isoformat()}"
            )
        if not self.sigma_by_lead:
            return False, f"model {self.model_name}@{self.model_version} has no usable spread"
        return True, None

    def summary(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "training_cutoff_utc": self.training_cutoff_utc.isoformat(),
            "created_at_utc": self.created_at_utc.isoformat(),
            "source_data_hash": self.source_data_hash,
            "spec_version": self.spec_version,
            "n_pairs": self.n_pairs,
            "n_dates": self.n_dates,
            "train_start": str(self.train_start) if self.train_start else None,
            "train_end": str(self.train_end) if self.train_end else None,
            "bias_f": round(self.bias_f, 6),
            "sigma_by_lead": {k: round(v, 6) for k, v in sorted(self.sigma_by_lead.items())},
        }

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["training_cutoff_utc"] = self.training_cutoff_utc.isoformat()
        payload["created_at_utc"] = self.created_at_utc.isoformat()
        payload["train_start"] = self.train_start.isoformat() if self.train_start else None
        payload["train_end"] = self.train_end.isoformat() if self.train_end else None
        payload["fit_notes"] = list(self.fit_notes)
        return payload

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> ModelArtifact:
        required = ("model_name", "model_version", "training_cutoff_utc", "source_data_hash")
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"artifact is missing required field(s): {missing}")
        if int(payload.get("spec_version", -1)) != ARTIFACT_SPEC_VERSION:
            raise ValueError(
                f"artifact spec_version {payload.get('spec_version')!r} is not supported "
                f"(this build reads version {ARTIFACT_SPEC_VERSION}); refusing to guess"
            )
        return cls(
            model_name=str(payload["model_name"]),
            model_version=str(payload["model_version"]),
            training_cutoff_utc=datetime.fromisoformat(str(payload["training_cutoff_utc"])),
            created_at_utc=datetime.fromisoformat(str(payload["created_at_utc"])),
            source_data_hash=str(payload["source_data_hash"]),
            spec_version=int(payload["spec_version"]),
            bias_f=float(payload["bias_f"]),
            sigma_by_lead={str(k): float(v) for k, v in dict(payload["sigma_by_lead"]).items()},
            counts_by_lead={str(k): int(v) for k, v in dict(payload["counts_by_lead"]).items()},
            n_pairs=int(payload["n_pairs"]),
            n_dates=int(payload["n_dates"]),
            train_start=date.fromisoformat(str(payload["train_start"])) if payload.get("train_start") else None,
            train_end=date.fromisoformat(str(payload["train_end"])) if payload.get("train_end") else None,
            min_samples_per_bucket=int(payload.get("min_samples_per_bucket", 5)),
            fit_notes=tuple(str(x) for x in payload.get("fit_notes", ())),
        )


def fit_artifact(
    *,
    model_name: str,
    model_version: str,
    pairs: list[dict],
    training_cutoff_utc: datetime,
    apply_bias: bool,
    min_samples_per_bucket: int = 5,
    created_at_utc: datetime | None = None,
) -> ModelArtifact:
    """Fit one model and freeze the result.

    ``pairs`` must already be filtered to ``target_date <= training_cutoff`` -
    this function records the cutoff it was given and hashes exactly what it
    sees, so a caller that passes a wider window is visible in the hash rather
    than hidden behind a label.

    ``apply_bias=False`` pins the bias to zero, which is what makes the raw
    baselines and the calibrated one a genuine ablation: same spread, only the
    location differs.
    """
    from weatherquant.models.residual import fit_residual_model

    seen_after_cutoff = [
        p["target_date"] for p in pairs if p.get("target_date") and p["target_date"] > training_cutoff_utc.date()
    ]
    if seen_after_cutoff:
        raise ValueError(
            f"{len(seen_after_cutoff)} pair(s) fall after the training cutoff "
            f"{training_cutoff_utc.date()}; filtering is the caller's job and a "
            f"mislabeled cutoff is future leakage"
        )

    residual = fit_residual_model(
        pairs,
        model_id=model_name,
        min_samples_per_bucket=min_samples_per_bucket,
        bias=0.0 if not apply_bias else None,
    )
    return ModelArtifact(
        model_name=model_name,
        model_version=model_version,
        training_cutoff_utc=training_cutoff_utc,
        created_at_utc=created_at_utc or datetime.now(tz=UTC),
        source_data_hash=hash_pairs(pairs),
        spec_version=ARTIFACT_SPEC_VERSION,
        bias_f=residual.bias_f,
        sigma_by_lead=dict(residual.sigma_by_lead),
        counts_by_lead=dict(residual.counts_by_lead),
        n_pairs=residual.n_pairs,
        n_dates=residual.n_dates,
        train_start=residual.train_start,
        train_end=residual.train_end,
        min_samples_per_bucket=residual.min_samples_per_bucket,
        fit_notes=tuple(residual.fit_notes),
    )


def artifact_path(models_dir: str | Path, model_name: str, model_version: str) -> Path:
    return Path(models_dir) / model_name / f"{model_version}.json"


def save_artifact(artifact: ModelArtifact, models_dir: str | Path) -> Path:
    """Persist an artifact. Refuses to replace an existing version."""
    path = artifact_path(models_dir, artifact.model_name, artifact.model_version)
    if path.exists():
        raise FileExistsError(
            f"{path} already exists; model versions are immutable. Bump "
            f"{artifact.model_version} instead of editing it in place."
        )
    _atomic_write(path, json.dumps(artifact.to_json_dict(), indent=2, sort_keys=True))
    return path


def load_artifact(models_dir: str | Path, model_name: str, model_version: str) -> ModelArtifact:
    path = artifact_path(models_dir, model_name, model_version)
    if not path.exists():
        raise FileNotFoundError(f"model artifact not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    artifact = ModelArtifact.from_json_dict(payload)
    if artifact.model_name != model_name or artifact.model_version != model_version:
        raise ValueError(
            f"{path} declares {artifact.model_name}@{artifact.model_version} but was "
            f"loaded as {model_name}@{model_version}"
        )
    return artifact


def save_manifest(models_dir: str | Path, pinned: dict[str, str]) -> Path:
    """Pin ``{model_name: model_version}``. Versions must exist on disk."""
    for name, version in pinned.items():
        if not artifact_path(models_dir, name, version).exists():
            raise FileNotFoundError(f"cannot pin {name}@{version}: no such artifact")
    payload = {
        "spec_version": ARTIFACT_SPEC_VERSION,
        "pinned": dict(sorted(pinned.items())),
        "pinned_at_utc": datetime.now(tz=UTC).isoformat(),
    }
    path = Path(models_dir) / MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_manifest(models_dir: str | Path) -> dict[str, str]:
    """Read the pinned versions. A missing manifest pins nothing (and is an error)."""
    path = Path(models_dir) / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(
            f"no model manifest at {path}; run `weatherquant-research fit-models --pin` "
            "so the engine cannot choose a version at runtime"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("spec_version", -1)) != ARTIFACT_SPEC_VERSION:
        raise ValueError(f"manifest spec_version {payload.get('spec_version')!r} unsupported")
    return {str(k): str(v) for k, v in dict(payload.get("pinned", {})).items()}
