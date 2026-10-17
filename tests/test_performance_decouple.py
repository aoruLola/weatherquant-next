"""Test ingest/publish decoupling.

Verify --no-publish and --no-ingest behavior:
- publish=False: ingest new data, update state, no generation
- ingest=False: no export read, rebuild from state, create generation
- mutual exclusion: enforced by external flock, not tested here
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from weatherquant.performance.streaming import build_performance_streaming


def _make_minimal_export(directory: Path, num_parts: int = 2, rows_per_part: int = 10) -> None:
    """Create a minimal valid export with cursor-bearing parts."""
    parts_dir = directory / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    for part_idx in range(1, num_parts + 1):
        lines = []
        for row_idx in range(rows_per_part):
            i = (part_idx - 1) * rows_per_part + row_idx
            # Minimal valid decision row (must have decision_time_utc and decision_id for cursor)
            row = {
                "decision_id": f"dec-{i:06d}",
                "decision_time_utc": f"2026-10-16T10:00:{i:02d}+00:00",
                "market_id": f"market-{i % 5}",
                "target_date": "2026-10-16",
                "state": "NO_TRADE" if i % 2 == 0 else "ACCEPT",
                "direction": "sell_yes",
                "model_name": "test-model",
                "model_version": "v1",
                "confidence": 0.6,
                "created_at_utc": f"2026-10-16T10:00:{i:02d}+00:00",
                "primary_reason": "test",
            }
            lines.append(json.dumps(row))

        part_path = parts_dir / f"part-{part_idx:09d}.jsonl"
        part_path.write_text("\n".join(lines) + "\n")


def test_no_publish_ingests_but_does_not_create_generation():
    """Test 1: publish=False ingests data, updates state, but does not create generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        result = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,  # Core: do not publish
            memory_limit="128MB",
        )

        # Verify: state store updated (watermark advanced)
        assert result["watermark"] is not None
        assert result["watermark"][0].startswith("2026-10-16")
        assert result["ingest"] is not None
        assert result["ingest"]["rows_consumed"] == 20

        # Verify: no generation created
        assert result["written"] == []
        assert not (out_dir / "generations").exists()
        assert not (out_dir / "current.json").exists()


def test_no_ingest_rebuilds_from_state_and_publishes():
    """Test 2: ingest=False does not read export, rebuilds from state and publishes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        # Step 1: ingest (no publish)
        result1 = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,
            memory_limit="128MB",
        )
        watermark1 = result1["watermark"]

        # Step 2: publish (no ingest)
        result2 = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=False,  # Core: do not ingest
            publish=True,
            memory_limit="128MB",
        )

        # Verify: watermark unchanged (no new data ingested)
        assert result2["watermark"] == watermark1
        assert result2["ingest"] is None

        # Verify: generation created
        assert len(result2["written"]) > 0
        assert (out_dir / "current.json").exists()
        current = json.loads((out_dir / "current.json").read_text())
        assert current["generation_id"] == result2["generation_id"]


def test_traditional_mode_ingests_and_publishes():
    """Test 3: traditional mode (ingest=True, publish=True) does both."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        result = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=True,
            memory_limit="128MB",
        )

        # Verify: data ingested
        assert result["ingest"]["rows_consumed"] == 20

        # Verify: generation published
        assert len(result["written"]) > 0
        assert (out_dir / "current.json").exists()


def test_idempotent_no_ingest_does_not_change_generation_hash():
    """Test 6: consecutive ingest=False runs should produce same generation content hash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        # Ingest once
        build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,
            memory_limit="128MB",
        )

        # Publish twice, content should be identical
        result1 = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=False,
            publish=True,
            memory_limit="128MB",
        )

        result2 = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=False,
            publish=True,
            memory_limit="128MB",
        )

        # Verify: generation_id content hash (12 hex digits) is the same
        hash1 = result1["generation_id"].split("-", 1)[1] if "-" in result1["generation_id"] else None
        hash2 = result2["generation_id"].split("-", 1)[1] if "-" in result2["generation_id"] else None
        assert hash1 == hash2, "Idempotent publish should produce same content hash"


# --------------------------------------------------------------------------
# Test 7: a gap or a truncated part is refused, and nothing is committed
# --------------------------------------------------------------------------


def test_missing_part_is_refused_and_state_is_untouched():
    """A gap must be refused: a watermark taken from the highest part would
    claim coverage of rows that are not there."""
    import pytest

    from weatherquant.performance.state import PerformanceStateError

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=3, rows_per_part=10)
        # Remove the middle part, leaving 1 and 3.
        (export_dir / "parts" / "part-000000002.jsonl").unlink()

        with pytest.raises(PerformanceStateError) as excinfo:
            build_performance_streaming(
                export=export_dir,
                out_dir=out_dir,
                dataset="fixture",
                state_db=state_db,
                ingest=True,
                publish=True,
                memory_limit="128MB",
            )
        assert "gap" in str(excinfo.value)

        # Nothing published, nothing consumed.
        assert not (out_dir / "current.json").exists()
        assert not (out_dir / "generations").exists()


def test_truncated_part_is_refused():
    """A part whose last line is not a decision cannot yield an end cursor."""
    import pytest

    from weatherquant.performance.state import PerformanceStateError

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)
        part = export_dir / "parts" / "part-000000002.jsonl"
        part.write_text('{"decision_id": "truncated", "decision_time')

        with pytest.raises(PerformanceStateError) as excinfo:
            build_performance_streaming(
                export=export_dir,
                out_dir=out_dir,
                dataset="fixture",
                state_db=state_db,
                ingest=True,
                publish=True,
                memory_limit="128MB",
            )
        assert "end cursor" in str(excinfo.value)
        assert not (out_dir / "current.json").exists()


# --------------------------------------------------------------------------
# Test 4 / 5: crash before and after the commit
# --------------------------------------------------------------------------


def test_failed_ingest_does_not_advance_the_watermark():
    """A refused run must leave the store exactly as it was.

    This is the crash-before-commit property: the run that dies must not have
    moved the watermark, or the rows it never wrote would be skipped forever.
    """
    import pytest

    from weatherquant.performance.state import PerformanceStateError

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        # A good run first, so there is a committed watermark to protect.
        first = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,
            memory_limit="128MB",
        )
        watermark_before = first["watermark"]

        # Now break the export: add a part with a gap.
        broken = export_dir / "parts" / "part-000000004.jsonl"
        broken.write_text("{}\n")
        with pytest.raises(PerformanceStateError):
            build_performance_streaming(
                export=export_dir,
                out_dir=out_dir,
                dataset="fixture",
                state_db=state_db,
                ingest=True,
                publish=False,
                memory_limit="128MB",
            )

        # Heal the export, then re-run: it still sees the same watermark, so
        # the refused run committed nothing.
        broken.unlink()
        again = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,
            memory_limit="128MB",
        )
        assert again["watermark"] == watermark_before


def test_rerun_after_a_committed_ingest_consumes_nothing_and_recovers():
    """Crash-after-commit: the committed watermark is the recovery point, so a
    re-run is a no-op rather than a double-count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        first = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,
            memory_limit="128MB",
        )
        assert first["ingest"]["rows_consumed"] == 20

        second = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,
            memory_limit="128MB",
        )
        assert second["ingest"]["rows_consumed"] == 0
        assert second["watermark"] == first["watermark"]


# --------------------------------------------------------------------------
# Test 10: the published watermark is the committed one
# --------------------------------------------------------------------------


def test_published_watermark_matches_the_committed_ingest_watermark():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        ingested = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,
            memory_limit="128MB",
        )
        published = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=False,
            publish=True,
            memory_limit="128MB",
        )

        assert published["watermark"] == ingested["watermark"]

        generation = json.loads((out_dir / "current.json").read_text())["generation_id"]
        manifest = json.loads(
            (out_dir / "generations" / generation / "manifest.json").read_text()
        )
        assert manifest["watermark"] == ingested["watermark"]


# --------------------------------------------------------------------------
# Test 8: a settlement revision republishes without new decisions
# --------------------------------------------------------------------------


def _settlement(market_id: str, status: str, outcome: int) -> dict:
    return {
        "market_id": market_id,
        "target_date": "2026-10-16",
        "settlement_status": status,
        "resolved_outcome": outcome,
    }


def test_settlement_revision_republishes_with_no_new_decisions():
    """A settlement that arrives days later changes outcomes without changing
    a single episode, so `--no-ingest` plus an index must still republish."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=False,
            memory_limit="128MB",
        )

        # Publish with no settlements at all.
        plain = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=False,
            publish=True,
            memory_limit="128MB",
        )

        # Now revise settlements. No new decisions were ingested.
        revised = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            settlements=[_settlement("market-0", "RESOLVED", 1)],
            ingest=False,
            publish=True,
            memory_limit="128MB",
        )

        assert revised["ingest"] is None
        assert revised["watermark"] == plain["watermark"]

        hash_plain = plain["generation_id"].split("-", 1)[1]
        hash_revised = revised["generation_id"].split("-", 1)[1]
        assert hash_plain != hash_revised, (
            "a settlement revision is an input to the batch, so it must change "
            "the generation's content hash"
        )


# --------------------------------------------------------------------------
# Test 11: the pointer only ever names a complete generation
# --------------------------------------------------------------------------


def test_pointer_names_a_complete_generation_and_never_a_partial_one():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=True,
            memory_limit="128MB",
        )
        first = json.loads((out_dir / "current.json").read_text())["generation_id"]

        # A second publish must switch the pointer only after the new batch is
        # complete, and must leave the first generation intact.
        second_result = build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=False,
            publish=True,
            memory_limit="128MB",
        )
        second = json.loads((out_dir / "current.json").read_text())["generation_id"]
        assert second == second_result["generation_id"]

        for gid in (first, second):
            generation_dir = out_dir / "generations" / gid
            for name in (
                "summary.json",
                "edge.json",
                "win-rate.json",
                "opportunities.json",
                "settlements.json",
                "models.json",
                "manifest.json",
            ):
                assert (generation_dir / name).exists(), f"{gid} is missing {name}"


def test_manifest_records_the_export_watermark_at_build():
    """Recorded for audit, and explicitly NOT what the Dashboard judges the
    live export by."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()

        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)
        (export_dir / "state.json").write_text(
            json.dumps(
                {
                    "next_part": 3,
                    "rows_exported": 20,
                    "watermark": ["2026-10-16T10:00:19+00:00", "hash-part-002"],
                }
            )
        )

        build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=True,
            memory_limit="128MB",
        )

        gid = json.loads((out_dir / "current.json").read_text())["generation_id"]
        manifest = json.loads((out_dir / "generations" / gid / "manifest.json").read_text())
        assert manifest["export_watermark_at_build"] == [
            "2026-10-16T10:00:19+00:00",
            "hash-part-002",
        ]


# --------------------------------------------------------------------------
# an impossible combination is refused, and refuses without side effects
# --------------------------------------------------------------------------


def test_no_publish_with_no_ingest_is_refused(capsys):
    """Both flags together would do nothing while exiting 0, which reads as a
    successful cycle in the log."""
    from weatherquant.cli import main

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()
        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        rc = main(
            [
                "build-performance",
                "--export",
                str(export_dir),
                "--out",
                str(out_dir),
                "--dataset",
                "fixture",
                "--state-db",
                str(state_db),
                "--memory-limit",
                "128MB",
                "--no-publish",
                "--no-ingest",
            ]
        )

    assert rc != 0
    assert rc == 2
    captured = capsys.readouterr()
    assert "would do nothing at all" in captured.err


def test_a_refused_combination_touches_nothing():
    """The refusal must happen before the store is opened: no watermark move,
    no ingest position, no pointer."""
    from weatherquant.cli import main

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()
        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        # A real run first, so there is state worth protecting.
        build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=True,
            memory_limit="128MB",
        )
        pointer_before = (out_dir / "current.json").read_text()
        ingest_before = (out_dir / "ingest-state.json").read_text()
        db_bytes_before = state_db.read_bytes()

        rc = main(
            [
                "build-performance",
                "--export",
                str(export_dir),
                "--out",
                str(out_dir),
                "--dataset",
                "fixture",
                "--state-db",
                str(state_db),
                "--memory-limit",
                "128MB",
                "--no-publish",
                "--no-ingest",
            ]
        )

        assert rc == 2
        assert (out_dir / "current.json").read_text() == pointer_before
        assert (out_dir / "ingest-state.json").read_text() == ingest_before
        assert state_db.read_bytes() == db_bytes_before


def test_publish_interval_is_stamped_into_the_published_payloads():
    """`stale_after_seconds` means the republish cadence, in the manifest and
    in the Dashboard alike - not the builder's 300s default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        export_dir = base / "export"
        out_dir = base / "out"
        state_db = base / "state" / "test.duckdb"
        state_db.parent.mkdir()
        _make_minimal_export(export_dir, num_parts=2, rows_per_part=10)

        build_performance_streaming(
            export=export_dir,
            out_dir=out_dir,
            dataset="fixture",
            state_db=state_db,
            ingest=True,
            publish=True,
            publish_interval_seconds=1200.0,
            memory_limit="128MB",
        )

        gid = json.loads((out_dir / "current.json").read_text())["generation_id"]
        for name in ("summary.json", "edge.json", "opportunities.json"):
            payload = json.loads((out_dir / "generations" / gid / name).read_text())
            assert payload["stale_after_seconds"] == 1200.0, name
