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
