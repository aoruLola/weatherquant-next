"""Decision-export provenance verifier (WQN-26).

Read-only.  Does not open the shadow database, does not touch the engine
lease.  Walks ``decisions-export/parts`` and, for every part that has a
``part-XXXXXXXX.manifest.json`` sidecar, checks the content digest, row
count, ``export_seq`` range, the hash chain of previous manifests, and the
optional HMAC-SHA256 signature.  The verifier also detects legacy parts
that predate the first manifest, half-written parts, forgery attempts and
residual ``*.tmp`` files.

Threat model and rationale live in :doc:`export-provenance`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "VERIFY_BAD_SIGNATURE",
    "VERIFY_CHAIN_BROKEN",
    "VERIFY_DIGEST_MISMATCH",
    "VERIFY_LEGACY_UNATTESTED",
    "VERIFY_MALFORMED_MANIFEST",
    "VERIFY_MISSING_MANIFEST",
    "VERIFY_OK",
    "VERIFY_ROWCOUNT_MISMATCH",
    "VERIFY_SEQ_RANGE_MISMATCH",
    "VERIFY_STRAY_TMP_FILE",
    "VERIFY_UNKNOWN_PART",
    "VERIFY_UNSIGNED",
    "ProvenanceError",
    "ProvenanceReport",
    "ProvenanceResult",
    "ProvenanceStatus",
    "verify_export_provenance",
]


#: Per-part status codes.  ``OK`` and ``LEGACY_UNATTESTED`` are the only
#: two that count as "accepted" in the overall verdict.
VERIFY_OK = "OK"
VERIFY_LEGACY_UNATTESTED = "LEGACY_UNATTESTED"
VERIFY_MISSING_MANIFEST = "MISSING_MANIFEST"
VERIFY_DIGEST_MISMATCH = "DIGEST_MISMATCH"
VERIFY_ROWCOUNT_MISMATCH = "ROWCOUNT_MISMATCH"
VERIFY_SEQ_RANGE_MISMATCH = "SEQ_RANGE_MISMATCH"
VERIFY_CHAIN_BROKEN = "CHAIN_BROKEN"
VERIFY_BAD_SIGNATURE = "BAD_SIGNATURE"
VERIFY_UNSIGNED = "UNSIGNED"
VERIFY_STRAY_TMP_FILE = "STRAY_TMP_FILE"
VERIFY_UNKNOWN_PART = "UNKNOWN_PART"
VERIFY_MALFORMED_MANIFEST = "MALFORMED_MANIFEST"

ProvenanceStatus = Literal[
    "OK",
    "LEGACY_UNATTESTED",
    "MISSING_MANIFEST",
    "DIGEST_MISMATCH",
    "ROWCOUNT_MISMATCH",
    "SEQ_RANGE_MISMATCH",
    "CHAIN_BROKEN",
    "BAD_SIGNATURE",
    "UNSIGNED",
    "STRAY_TMP_FILE",
    "UNKNOWN_PART",
    "MALFORMED_MANIFEST",
]

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _index_of(path: Path) -> int:
    """Extract the part index (1-based) from either a part or manifest path.

    Handles both ``part-000000005.jsonl`` and ``part-000000005.manifest.json``.
    Returns 0 on names that do not match the naming contract (the caller
    already filters by the ``part-*.jsonl`` / ``part-*.manifest.json`` glob,
    so a 0 means something else is on disk and we should ignore it).
    """
    stem = path.stem  # drops one suffix; for .manifest.json we want the numeric
    if stem.endswith(".manifest"):
        stem = stem[: -len(".manifest")]
    try:
        return int(stem.split("-")[1])
    except (IndexError, ValueError):
        return 0


def _sha256_of(path: Path) -> str:
    """Full-file sha256, hex-encoded.  Raises ``OSError`` on unreadable files."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(obj: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding used for HMAC input.

    Sort keys + compact separators so field order never changes the output.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _hmac_sign(key: bytes, payload: dict[str, Any]) -> str:
    """Sign the canonical manifest (excluding ``hmac_sha256``) with HMAC-SHA256."""
    signing_payload = {k: v for k, v in payload.items() if k != "hmac_sha256"}
    return hmac.new(key, _canonical_json(signing_payload), hashlib.sha256).hexdigest()


def _hmac_verify(key: bytes, payload: dict[str, Any], presented: str | None) -> bool:
    expected = _hmac_sign(key, payload)
    if not presented:
        return False
    return hmac.compare_digest(expected, presented)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ProvenanceError:
    """A single finding for one part / one stray file."""

    path: str
    status: ProvenanceStatus
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "detail": self.detail}


@dataclass(slots=True)
class ProvenanceResult:
    """Verification outcome for one part.

    ``index`` is 1-based, matching the exporter's naming.  ``part_path`` may
    be empty for stray-tmp findings; ``manifest_path`` empty when the part
    has no sidecar.
    """

    index: int
    part_path: str | None = None
    manifest_path: str | None = None
    status: ProvenanceStatus = VERIFY_OK
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "part_path": self.part_path,
            "manifest_path": self.manifest_path,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ProvenanceReport:
    """Structured overall report returned by :func:`verify_export_provenance`."""

    parts_dir: str
    parts_checked: int = 0
    manifests_found: int = 0
    verified_ok: int = 0
    legacy_unattested: int = 0
    errors: list[ProvenanceResult] = field(default_factory=list)
    strays: list[ProvenanceError] = field(default_factory=list)
    verdict: str = VERDICT_PASS
    signing_key_configured: bool = False

    @property
    def ok(self) -> bool:
        return self.verdict == VERDICT_PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "parts_dir": self.parts_dir,
            "parts_checked": self.parts_checked,
            "manifests_found": self.manifests_found,
            "verified_ok": self.verified_ok,
            "legacy_unattested": self.legacy_unattested,
            "errors": [e.as_dict() for e in self.errors],
            "strays": [s.as_dict() for s in self.strays],
            "verdict": self.verdict,
            "signing_key_configured": self.signing_key_configured,
        }


def verify_export_provenance(
    parts_dir: str | Path,
    *,
    signing_key: str | None = None,
) -> ProvenanceReport:
    """Walk ``parts_dir`` and return a structured :class:`ProvenanceReport`.

    ``signing_key`` may be ``None`` (unsigned, or to be read from
    ``WQ_EXPORT_SIGNING_KEY``).  When both are unset the verifier still
    checks digests, chain and structural integrity; it simply does not
    know whether a missing / wrong signature matters.
    """
    directory = Path(parts_dir)
    key = signing_key
    if key is None:
        key = os.environ.get("WQ_EXPORT_SIGNING_KEY")
    key_bytes = key.encode("utf-8") if key else None

    report = ProvenanceReport(
        parts_dir=str(directory),
        signing_key_configured=key_bytes is not None,
    )

    if not directory.is_dir():
        # Nothing to verify.  Returning an empty PASS is slightly misleading,
        # but a missing export is an operational condition the caller
        # should discover from other signals; provenance is not the place
        # to invent a failure mode for it.  (A real production export
        # always has at least a state.json by the time anything else
        # notices it.)
        return report

    # Enumeration.  Part files are the ground truth; manifests are sidecars.
    parts = sorted([p for p in directory.glob("part-*.jsonl") if _index_of(p) > 0])
    manifests = sorted([m for m in directory.glob("part-*.manifest.json") if _index_of(m) > 0])
    report.parts_checked = len(parts)
    report.manifests_found = len(manifests)

    manifest_by_index: dict[int, Path] = {_index_of(m): m for m in manifests}
    manifest_contents: dict[int, dict[str, Any]] = {}
    manifest_raw_bytes: dict[int, bytes] = {}

    for index, path in manifest_by_index.items():
        try:
            raw = path.read_bytes()
            manifest_raw_bytes[index] = raw
            manifest_contents[index] = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as exc:
            report.errors.append(
                ProvenanceResult(
                    index=index,
                    manifest_path=str(path),
                    status=VERIFY_MALFORMED_MANIFEST,
                    detail=f"manifest is unreadable or not valid JSON: {type(exc).__name__}: {exc}",
                )
            )

    # Stray .tmp files left over from a half-write.
    for tmp in sorted(directory.glob("*.tmp")):
        report.strays.append(
            ProvenanceError(
                path=str(tmp),
                status=VERIFY_STRAY_TMP_FILE,
                detail="residual temp file; a write did not complete cleanly",
            )
        )

    if not parts and not manifests:
        return report

    # Determine the first manifested part: everything with a lower index
    # is LEGACY_UNATTESTED (and accepted).
    first_manifested_index = min(manifest_by_index) if manifest_by_index else None

    for part_path in parts:
        idx = _index_of(part_path)
        res = _verify_part(
            idx=idx,
            part_path=part_path,
            manifest_path=manifest_by_index.get(idx),
            manifest=manifest_contents.get(idx),
            prev_manifest_raw=manifest_raw_bytes.get(idx - 1) if idx - 1 in manifest_raw_bytes else None,
            first_manifested_index=first_manifested_index,
            key_bytes=key_bytes,
        )
        if res.status == VERIFY_OK:
            report.verified_ok += 1
        elif res.status == VERIFY_LEGACY_UNATTESTED:
            report.legacy_unattested += 1
        else:
            report.errors.append(res)

    # Manifest-only entries (someone wrote a manifest without a part).
    for idx, mpath in manifest_by_index.items():
        if any(_index_of(p) == idx for p in parts):
            continue
        # Manifest references a part we don't have; treat as chain break too.
        report.errors.append(
            ProvenanceResult(
                index=idx,
                manifest_path=str(mpath),
                status=VERIFY_UNKNOWN_PART,
                detail="manifest present but part file is missing",
            )
        )

    # Overall verdict.
    any_error = bool(report.errors) or bool(report.strays)
    report.verdict = VERDICT_FAIL if any_error else VERDICT_PASS
    return report


# ---------------------------------------------------------------------------
# Per-part checks
# ---------------------------------------------------------------------------

def _verify_part(
    *,
    idx: int,
    part_path: Path,
    manifest_path: Path | None,
    manifest: dict[str, Any] | None,
    prev_manifest_raw: bytes | None,
    first_manifested_index: int | None,
    key_bytes: bytes | None,
) -> ProvenanceResult:
    """Run all verifications for one part, return the first status that fails."""
    result = ProvenanceResult(index=idx, part_path=str(part_path))
    if manifest_path is not None:
        result.manifest_path = str(manifest_path)

    # --- Legacy detection ------------------------------------------------
    if manifest_path is None:
        if first_manifested_index is None or idx < first_manifested_index:
            result.status = VERIFY_LEGACY_UNATTESTED
            result.detail = "part predates the first manifest; accepted as legacy"
            return result
        result.status = VERIFY_MISSING_MANIFEST
        result.detail = (
            f"a manifest exists at index {first_manifested_index}, so this part "
            "is expected to have one but does not"
        )
        return result

    # At this point manifest_path is present and JSON has been parsed.
    if manifest is None:
        # Already flagged as MALFORMED_MANIFEST above.
        result.status = VERIFY_MALFORMED_MANIFEST
        result.detail = "manifest could not be parsed"
        return result

    # --- Structural checks on the manifest fields -----------------------
    schema_version = manifest.get("schema_version")
    if schema_version != 1:
        result.status = VERIFY_MALFORMED_MANIFEST
        result.detail = (
            f"schema_version={schema_version!r}; only 1 is currently supported"
        )
        return result

    declared_index = manifest.get("index")
    if declared_index != idx:
        result.status = VERIFY_MALFORMED_MANIFEST
        result.detail = (
            f"manifest.index={declared_index!r} does not match file stem index {idx}"
        )
        return result

    declared_part = manifest.get("part_file")
    expected_part_file = f"part-{idx:09d}.jsonl"
    if declared_part != expected_part_file:
        result.status = VERIFY_MALFORMED_MANIFEST
        result.detail = (
            f"manifest.part_file={declared_part!r}, expected {expected_part_file!r}"
        )
        return result

    # --- Part content: digest ------------------------------------------
    try:
        actual_digest = _sha256_of(part_path)
    except OSError as exc:
        result.status = VERIFY_DIGEST_MISMATCH
        result.detail = f"cannot read part file: {type(exc).__name__}: {exc}"
        return result
    declared_digest = manifest.get("part_sha256")
    if not isinstance(declared_digest, str) or len(declared_digest) != 64:
        result.status = VERIFY_MALFORMED_MANIFEST
        result.detail = "manifest.part_sha256 is missing or malformed"
        return result
    if not hmac.compare_digest(actual_digest, declared_digest):
        result.status = VERIFY_DIGEST_MISMATCH
        result.detail = (
            f"part content sha256 mismatch: manifest={declared_digest[:16]}..., "
            f"actual={actual_digest[:16]}..."
        )
        return result

    # --- Row count + seq range ------------------------------------------
    row_count, first_seq, last_seq, first_ts, last_ts = _scan_part(part_path)
    declared_count = manifest.get("row_count")
    if declared_count != row_count:
        result.status = VERIFY_ROWCOUNT_MISMATCH
        result.detail = (
            f"row_count mismatch: manifest={declared_count}, actual={row_count}"
        )
        return result
    declared_first = manifest.get("first_export_seq")
    declared_last = manifest.get("last_export_seq")
    if (
        declared_first != first_seq
        or declared_last != last_seq
    ):
        result.status = VERIFY_SEQ_RANGE_MISMATCH
        result.detail = (
            f"export_seq range mismatch: manifest=({declared_first}, {declared_last}), "
            f"actual=({first_seq}, {last_seq})"
        )
        return result
    declared_first_ts = manifest.get("first_decision_time_utc")
    declared_last_ts = manifest.get("last_decision_time_utc")
    if declared_first_ts != first_ts or declared_last_ts != last_ts:
        result.status = VERIFY_SEQ_RANGE_MISMATCH
        result.detail = (
            f"decision_time_utc range mismatch: "
            f"manifest=({declared_first_ts}, {declared_last_ts}), actual=({first_ts}, {last_ts})"
        )
        return result

    # --- Hash chain ------------------------------------------------------
    declared_prev = manifest.get("prev_manifest_sha256")
    if prev_manifest_raw is None:
        if declared_prev not in (None, "", "null"):
            result.status = VERIFY_CHAIN_BROKEN
            result.detail = (
                f"first manifest in sequence declares prev_manifest_sha256="
                f"{declared_prev!r}, expected null"
            )
            return result
    else:
        expected_prev_digest = _sha256_of_bytes(prev_manifest_raw)
        if not isinstance(declared_prev, str) or len(declared_prev) != 64:
            result.status = VERIFY_CHAIN_BROKEN
            result.detail = "prev_manifest_sha256 is missing or malformed"
            return result
        if not hmac.compare_digest(declared_prev, expected_prev_digest):
            result.status = VERIFY_CHAIN_BROKEN
            result.detail = (
                f"prev_manifest_sha256 mismatch: manifest={declared_prev[:16]}..., "
                f"actual-prev={expected_prev_digest[:16]}..."
            )
            return result

    # --- Signature -------------------------------------------------------
    presented_sig = manifest.get("hmac_sha256")
    if key_bytes is None:
        # No key configured at all: we can't validate the signature.
        # An explicit signature would still be nice-to-have info; the lack
        # of one does NOT make this part fail, because unsigned is a
        # deliberate mode (see the design doc).
        result.status = VERIFY_OK
        result.detail = "ok (unsigned; no signing key configured)"
        return result

    if not presented_sig:
        result.status = VERIFY_UNSIGNED
        result.detail = "WQ_EXPORT_SIGNING_KEY is configured but manifest has no hmac_sha256"
        return result

    if not _hmac_verify(key_bytes, manifest, presented_sig):
        result.status = VERIFY_BAD_SIGNATURE
        result.detail = "HMAC-SHA256 signature does not match the canonical manifest"
        return result

    result.status = VERIFY_OK
    result.detail = "ok (signed)"
    return result


# ---------------------------------------------------------------------------
# Part scanner
# ---------------------------------------------------------------------------

def _scan_part(
    part_path: Path,
) -> tuple[int, int | None, int | None, str | None, str | None]:
    """Return ``(row_count, first_seq, last_seq, first_ts, last_ts)``.

    Counts only non-blank lines; skips lines that are not JSON objects.
    Sequences can be ``None`` for legacy v1 rows that predate the column.
    """
    row_count = 0
    first_seq: int | None = None
    last_seq: int | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    with part_path.open("rb") as raw:
        text = raw.read().decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        row_count += 1
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        seq = obj.get("export_seq")
        if seq is not None:
            try:
                seq_int = int(seq)
            except (TypeError, ValueError):
                seq_int = None
            if seq_int is not None:
                if first_seq is None:
                    first_seq = seq_int
                last_seq = seq_int
        ts = obj.get("decision_time_utc")
        if isinstance(ts, str):
            if first_ts is None:
                first_ts = ts
            last_ts = ts
    return row_count, first_seq, last_seq, first_ts, last_ts
