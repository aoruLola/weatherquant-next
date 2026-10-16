"""The state store: what survives a crash, what is refused, what is rebuilt.

The streaming engine's correctness rests on a handful of properties that are
easy to state and easy to get subtly wrong, so each one is a test rather than a
comment:

* the watermark advances only after the analysis is committed;
* a part that changes underneath the store is refused, not re-read;
* a gap in the part sequence is refused, not skipped;
* a store built by a different input or algorithm is not extended;
* re-input is idempotent;
* a settlement that arrives later changes the win rate without re-reading a
  single decision.
"""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest

from test_performance_streaming import (
    NOW,
    _dataset,
    _read_generation,
    _settlements,
    _strip,
    _write_parts,
)
from weatherquant.performance.snapshot import resolve_generation
from weatherquant.performance.state import (
    PerformanceStateError,
    PerformanceStateStore,
    StateMismatch,
)
from weatherquant.performance.streaming import (
    build_performance_streaming,
    ingest_export,
    scan_export_parts,
)


def _ingest(tmp_path: Path, rows, *, parts: int = 4, state_db: Path | None = None, **kwargs):
    """Write the export and ingest it, for tests that do not tamper in between."""
    export = _write_parts(tmp_path, rows, parts)
    return _ingest_export(export, state_db or (tmp_path / "state.duckdb"), **kwargs)


def _ingest_export(export: Path, state_db: Path, **kwargs):
    """Ingest an export that is already on disk, exactly as it is."""
    with PerformanceStateStore(state_db) as store:
        return ingest_export(store, export, **kwargs)


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parts", [1, 2, 5, 13])
def test_any_number_of_parts_gives_the_same_snapshots(tmp_path, parts) -> None:
    """Multi-batch == single batch, for every split of the same input."""
    rows = _dataset(seed=11, rows=600)
    settlements = _settlements(rows)
    build_performance_streaming(
        export=_write_parts(tmp_path, rows, parts),
        out_dir=tmp_path / "performance",
        state_db=tmp_path / "state.duckdb",
        dataset="fixture",
        settlements=settlements,
        now=NOW,
    )
    streamed = _strip(_read_generation(tmp_path / "performance"))

    build_performance_streaming(
        export=_write_parts(tmp_path, rows, 1),
        out_dir=tmp_path / "single",
        state_db=tmp_path / "single.duckdb",
        dataset="fixture",
        settlements=settlements,
        now=NOW,
    )
    single = _strip(_read_generation(tmp_path / "single"))

    assert streamed == single


def test_a_part_boundary_does_not_split_an_episode(tmp_path) -> None:
    """An episode that straddles two parts is one episode, not two.

    The dataset is written so that one key is decided continuously across the
    whole history; splitting it anywhere must not change the episode count.
    """
    rows = _dataset(seed=5, rows=400)
    settlements = _settlements(rows)
    reference = None
    for parts in (1, 3, 7):
        build_performance_streaming(
            export=_write_parts(tmp_path, rows, parts),
            out_dir=tmp_path / f"p{parts}",
            state_db=tmp_path / f"p{parts}.duckdb",
            dataset="fixture",
            settlements=settlements,
            now=NOW,
        )
        payloads = _strip(_read_generation(tmp_path / f"p{parts}"))
        if reference is None:
            reference = payloads
        else:
            assert payloads == reference


# ---------------------------------------------------------------------------
# crash, resume, idempotency
# ---------------------------------------------------------------------------


def test_an_interrupted_ingest_resumes_without_double_counting(tmp_path, monkeypatch) -> None:
    """A crash mid-part leaves the store at the previous boundary.

    The failure is injected inside the transaction, which is the same thing a
    kill -9 does to it: the rows of the part being read were never committed,
    so the next run reads them again and counts them once.
    """
    import weatherquant.performance.streaming as streaming

    rows = _dataset(seed=21, rows=800)
    export = _write_parts(tmp_path, rows, 4)
    state_db = tmp_path / "state.duckdb"

    original = streaming._iter_rows
    calls = {"n": 0}

    def die_on_the_second_part(path):
        calls["n"] += 1
        for index, row in enumerate(original(path)):
            if calls["n"] == 2 and index >= 50:
                raise RuntimeError("simulated kill -9")
            yield row

    monkeypatch.setattr(streaming, "_iter_rows", die_on_the_second_part)
    with pytest.raises(RuntimeError, match="simulated kill"):
        _ingest_export(export, state_db)

    with PerformanceStateStore(state_db) as store:
        state = store.load_state()
        assert state is not None
        assert state.last_part_index == 1, "the watermark must not have advanced"
        assert state.rows_consumed == 200, "only the committed part may be counted"
        assert set(store.recorded_parts()) == {1}

    monkeypatch.setattr(streaming, "_iter_rows", original)
    result = _ingest_export(export, state_db)
    assert result.rows_consumed == 600, "the remaining three parts"
    assert result.caught_up is False

    # A third run has nothing left to do and must not move anything.
    again = _ingest_export(export, state_db)
    assert again.rows_consumed == 0
    assert again.caught_up is True
    with PerformanceStateStore(state_db) as store:
        state = store.load_state()
        assert state.rows_consumed == 800
        assert state.last_part_index == 4


def test_repeating_the_same_input_is_idempotent(tmp_path) -> None:
    rows = _dataset(seed=31, rows=500)
    state_db = tmp_path / "state.duckdb"

    first = _ingest(tmp_path, rows, parts=3, state_db=state_db)
    second = _ingest(tmp_path, rows, parts=3, state_db=state_db)

    assert first.rows_consumed == 500
    assert second.rows_consumed == 0
    with PerformanceStateStore(state_db) as store:
        state = store.load_state()
        assert state.rows_consumed == 500
        assert state.decisions_seen == 500
        assert state.accepts_seen == first.state["accepts_seen"]
        assert len(store.recorded_parts()) == 3


def test_a_missing_part_is_refused_and_the_watermark_does_not_move(tmp_path) -> None:
    rows = _dataset(seed=41, rows=400)
    export = _write_parts(tmp_path, rows, 4)
    state_db = tmp_path / "state.duckdb"

    _ingest_export(export, state_db)
    (export / "parts" / "part-000000002.jsonl").unlink()

    with pytest.raises(PerformanceStateError, match="gap"):
        _ingest_export(export, state_db)

    with PerformanceStateStore(state_db) as store:
        state = store.load_state()
        assert state.last_part_index == 4, "a refusal must not advance the cursor"
        assert state.rows_consumed == 400


def test_a_part_that_changed_after_being_consumed_is_refused(tmp_path) -> None:
    """A rewritten part is a different export wearing the same name."""
    rows = _dataset(seed=51, rows=400)
    export = _write_parts(tmp_path, rows, 4)
    state_db = tmp_path / "state.duckdb"
    _ingest_export(export, state_db)

    # Rewrite the last part with different content but the same row count.
    part = export / "parts" / "part-000000004.jsonl"
    replacement = [_dataset(seed=99, rows=100)]
    part.write_text(
        "".join(json.dumps(row) + "\n" for row in replacement[0]), encoding="utf-8"
    )

    with pytest.raises(PerformanceStateError, match="changed after it was consumed"):
        _ingest_export(export, state_db)


def test_a_part_out_of_cursor_order_is_refused(tmp_path) -> None:
    """The fold assumes the export is non-decreasing; a violation is refused.

    This is also the shape of the REPLAY risk: a row written later with an
    earlier business time. Refusing is the honest answer - folding it would
    invent episode boundaries that are not there.
    """
    rows = _dataset(seed=61, rows=200)
    export = _write_parts(tmp_path, rows, 2)
    parts = sorted((export / "parts").glob("part-*.jsonl"))
    # Swap the two parts' contents: the second part now holds the earlier rows.
    first = parts[0].read_text(encoding="utf-8")
    second = parts[1].read_text(encoding="utf-8")
    parts[0].write_text(second, encoding="utf-8")
    parts[1].write_text(first, encoding="utf-8")

    with pytest.raises(PerformanceStateError, match="not in cursor order"):
        _ingest_export(export, tmp_path / "state.duckdb")


# ---------------------------------------------------------------------------
# parameters and rebuild
# ---------------------------------------------------------------------------


def test_a_changed_fold_parameter_is_refused(tmp_path) -> None:
    rows = _dataset(seed=71, rows=300)
    state_db = tmp_path / "state.duckdb"
    _ingest(tmp_path, rows, parts=2, state_db=state_db)

    with pytest.raises(StateMismatch, match="gap_window_seconds"):
        _ingest(tmp_path, rows, parts=2, state_db=state_db, gap_window_seconds=60.0)

    with pytest.raises(StateMismatch, match="was built from"):
        _ingest(tmp_path / "other", rows, parts=2, state_db=state_db)


def test_reset_rebuilds_the_store_from_the_export(tmp_path) -> None:
    rows = _dataset(seed=81, rows=300)
    state_db = tmp_path / "state.duckdb"
    _ingest(tmp_path, rows, parts=2, state_db=state_db)

    with PerformanceStateStore(state_db) as store:
        store.reset()
        assert store.load_state() is None
        assert store.recorded_parts() == {}

    result = _ingest(tmp_path, rows, parts=2, state_db=state_db)
    assert result.rows_consumed == 300
    with PerformanceStateStore(state_db) as store:
        assert store.load_state().rows_consumed == 300


def test_a_second_process_is_refused_rather_than_corrupting_the_store(tmp_path) -> None:
    """DuckDB allows one writer *process*; a second one must be told, not crash.

    Two connections inside one process share an instance, so the interesting
    case is a second process - which is what two overlapping CLI runs are.
    """
    import subprocess
    import sys

    rows = _dataset(seed=91, rows=200)
    _write_parts(tmp_path, rows, 2)
    state_db = tmp_path / "state.duckdb"
    with PerformanceStateStore(state_db):
        attempt = subprocess.run(
            [sys.executable, "-c", f"import duckdb; duckdb.connect({str(state_db)!r})"],
            capture_output=True,
            text=True,
        )
    assert attempt.returncode != 0
    message = (attempt.stderr or attempt.stdout).lower()
    # The wording is platform-specific - DuckDB says "Conflicting lock is held"
    # on Linux and "File is already open in" on Windows - so asserting one of
    # them makes this test pass or fail on the operating system rather than on
    # the behaviour. What has to hold is that the second process is refused,
    # and refused for *this* reason rather than an unrelated one.
    assert "lock" in message or "already open" in message, message


# ---------------------------------------------------------------------------
# settlements and partial recompute
# ---------------------------------------------------------------------------


def test_a_settlement_revision_updates_the_result_without_reingesting(tmp_path) -> None:
    """The settlement layer is recomputed; the episode layer is not."""
    rows = _dataset(seed=101, rows=600)
    settlements = _settlements(rows)
    export = _write_parts(tmp_path, rows, 3)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"

    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=settlements, now=NOW,
    )
    before = _read_generation(out)["win-rate.json"]["data"]["overall"]
    assert before["valid_samples"] > 0

    # Every market flips its outcome.
    flipped = [
        {**row, "resolved_outcome": 0.0 if row["resolved_outcome"] == 1.0 else 1.0}
        for row in settlements
    ]
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=flipped, now=NOW,
    )
    after = _read_generation(out)["win-rate.json"]["data"]["overall"]
    assert after["valid_samples"] == before["valid_samples"]
    assert after["counts"]["WIN"] == before["counts"]["LOSS"]
    assert after["counts"]["LOSS"] == before["counts"]["WIN"]

    # And a run with no index at all keeps the answers it already has.
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture", now=NOW,
    )
    kept = _read_generation(out)["win-rate.json"]["data"]["overall"]
    assert kept == after


def test_rebuilding_without_touching_the_export_is_supported(tmp_path) -> None:
    """``ingest=False`` recomputes the snapshots from the store alone."""
    rows = _dataset(seed=111, rows=400)
    export = _write_parts(tmp_path, rows, 2)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=_settlements(rows), now=NOW,
    )
    first = _read_generation(out)

    result = build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        ingest=False, now=NOW,
    )
    assert result["ingest"] is None
    second = _read_generation(out)
    assert _strip(second) == _strip(first)


# ---------------------------------------------------------------------------
# settlement provenance: a kept answer must not look like a fresh one
# ---------------------------------------------------------------------------


def test_a_kept_settlement_answer_carries_its_retrieval_time(tmp_path) -> None:
    """Requirement 2: keeping an answer must not disguise its age.

    The second run supplies no index at all. The win rate must still be there -
    that is the point of the sticky layer - but the payload has to say when the
    answers were retrieved, so a reader can tell "kept" from "just verified".
    """
    rows = _dataset(seed=171, rows=400)
    index = _settlements(rows)
    export = _write_parts(tmp_path, rows, 2)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"

    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=index, now=NOW,
    )
    first = _read_generation(out)
    answers = first["settlements.json"]["data"]["answers"]
    assert answers and all(row["fetched_at_utc"] for row in answers)
    assert all(row["verification_status"] == "VERIFIED" for row in answers)
    assert all(row["resolution_source"] for row in answers) is False, (
        "this fixture records no source; NULL means 'not recorded', and it must "
        "stay NULL rather than be filled in"
    )
    freshness = first["settlements.json"]["data"]["freshness"]
    assert freshness["answers_without_retrieval_time"] == 0
    assert freshness["as_of_utc"] is not None
    assert freshness["oldest_utc"] is not None
    assert first["summary.json"]["data"]["settlement_freshness"] == freshness

    # No index this time.
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture", now=NOW,
    )
    second = _read_generation(out)
    assert second["win-rate.json"]["data"]["overall"]["valid_samples"] == (
        first["win-rate.json"]["data"]["overall"]["valid_samples"]
    )
    assert second["settlements.json"]["data"]["freshness"] == freshness
    assert second["settlements.json"]["data"]["answers"] == answers


def test_an_answer_with_no_retrieval_time_says_so(tmp_path) -> None:
    """A stored answer without a timestamp must not read as freshly verified."""
    rows = _dataset(seed=181, rows=200)
    index = [{key: value for key, value in row.items() if key != "fetched_at_utc"} for row in _settlements(rows)]
    export = _write_parts(tmp_path, rows, 2)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=index, now=NOW,
    )
    payload = _read_generation(out)["settlements.json"]
    freshness = payload["data"]["freshness"]
    assert freshness["answers_without_retrieval_time"] == freshness["answers"]
    assert freshness["as_of_utc"] is None
    assert any("retrieval time" in warning for warning in payload["warnings"])


def test_only_the_revised_market_changes(tmp_path) -> None:
    """Requirement 3: a revision recomputes what it touches, and only that."""
    rows = _dataset(seed=191, rows=600)
    index = _settlements(rows)
    export = _write_parts(tmp_path, rows, 3)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=index, now=NOW,
    )
    before = _read_generation(out)
    before_episodes = {
        row["episode_id"]: row["outcome"]
        for row in before["opportunities.json"]["data"]["episodes"]
    }
    target = index[0]
    flipped = [
        {**row, "resolved_outcome": 1.0 - row["resolved_outcome"]}
        if (row["market_id"], row["target_date"]) == (target["market_id"], target["target_date"])
        else row
        for row in index
    ]
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=flipped, now=NOW,
    )
    after = _read_generation(out)
    after_episodes = {
        row["episode_id"]: row["outcome"]
        for row in after["opportunities.json"]["data"]["episodes"]
    }
    changed = {key for key in before_episodes if before_episodes[key] != after_episodes[key]}
    affected = {
        key
        for key in before_episodes
        if before["opportunities.json"]["data"]["episodes"]
        and next(
            row["market_id"]
            for row in before["opportunities.json"]["data"]["episodes"]
            if row["episode_id"] == key
        )
        == target["market_id"]
    }
    assert changed, "the revised market must change something"
    assert changed <= affected, "no other market may move"
    assert before["summary.json"]["data"]["accept_observations"] == (
        after["summary.json"]["data"]["accept_observations"]
    ), "a settlement revision must not change a single episode aggregate"


def test_a_withdrawn_settlement_is_retracted_and_not_resurrected(tmp_path) -> None:
    """Requirement 4: an explicit withdrawal must survive later runs.

    A union can express "this market resolved" but never "this market is no
    longer scored", so retraction is an explicit act. Once retracted, the
    answer must stay gone - a later run that supplies no index must not bring
    it back from the cache.
    """
    rows = _dataset(seed=201, rows=600)
    index = _settlements(rows)
    export = _write_parts(tmp_path, rows, 3)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=index, now=NOW,
    )
    before = _read_generation(out)
    assert len(before["settlements.json"]["data"]["answers"]) == len(index)

    withdrawn = index[0]
    remaining = [
        row
        for row in index
        if (row["market_id"], row["target_date"])
        != (withdrawn["market_id"], withdrawn["target_date"])
    ]
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=remaining, settlements_replace=True, now=NOW,
    )
    after = _read_generation(out)
    answers = after["settlements.json"]["data"]["answers"]
    assert len(answers) == len(remaining)
    assert not any(
        row["market_id"] == withdrawn["market_id"]
        and row["target_date"] == withdrawn["target_date"]
        for row in answers
    )
    # Only the withdrawn (market, target date) loses its outcome; the same
    # market's other days keep theirs, because a retraction is per answer.
    retracted = [
        row
        for row in after["opportunities.json"]["data"]["episodes"]
        if row["market_id"] == withdrawn["market_id"]
        and row["target_date"] == withdrawn["target_date"]
    ]
    assert retracted and all(row["outcome"] == "UNAVAILABLE" for row in retracted)
    still_scored = [
        row
        for row in after["opportunities.json"]["data"]["episodes"]
        if row["market_id"] == withdrawn["market_id"]
        and row["target_date"] != withdrawn["target_date"]
    ]
    assert any(row["outcome"] in {"WIN", "LOSS"} for row in still_scored)

    # A later run with no index keeps the retraction.
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture", now=NOW,
    )
    kept = _read_generation(out)["settlements.json"]["data"]["answers"]
    assert len(kept) == len(remaining)
    assert not any(
        row["market_id"] == withdrawn["market_id"]
        and row["target_date"] == withdrawn["target_date"]
        for row in kept
    )


def test_an_empty_settlement_index_retracts_nothing(tmp_path) -> None:
    """`--settlements-replace` with an empty index must be inert, not a wipe.

    Replace makes the index authoritative, so an empty one reads as "no
    settlement exists" and would delete every answer - including ones the
    platform confirmed. An index comes out empty for ordinary reasons: the
    backfill matched no markets, or the platform was unreachable. Neither is
    an assertion that yesterday's answers were wrong.

    The guard is `if answers:` before the write. Without a test it is one
    refactor away from being "tidied" into an unconditional call, and the
    loss would be silent: the snapshots would simply stop scoring, with
    `UNAVAILABLE` where a resolved market used to be.
    """
    rows = _dataset(seed=211, rows=600)
    index = _settlements(rows)
    export = _write_parts(tmp_path, rows, 3)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"
    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=index, now=NOW,
    )
    before = _read_generation(out)
    assert len(before["settlements.json"]["data"]["answers"]) == len(index)

    build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=[], settlements_replace=True, now=NOW,
    )

    after = _read_generation(out)
    assert len(after["settlements.json"]["data"]["answers"]) == len(index), (
        "an empty index wiped the settlement layer"
    )
    outcomes = {
        row["outcome"] for row in after["opportunities.json"]["data"]["episodes"]
    }
    assert outcomes & {"WIN", "LOSS"}, "an empty index un-scored every episode"


def test_the_settlement_layer_records_where_it_came_from(tmp_path) -> None:
    """Requirement 5: source, retrieval time, verdict and version all travel."""
    rows = _dataset(seed=211, rows=200)
    index = [
        {**row, "resolution_source": "clob:/v2/resolutions", "verification_status": "UNVERIFIED"}
        for row in _settlements(rows, verified=False)
    ]
    export = _write_parts(tmp_path, rows, 2)
    out = tmp_path / "performance"
    build_performance_streaming(
        export=export, out_dir=out, state_db=tmp_path / "state.duckdb", dataset="fixture",
        settlements=index, now=NOW,
    )
    payload = _read_generation(out)["settlements.json"]["data"]
    assert all(row["resolution_source"] == "clob:/v2/resolutions" for row in payload["answers"])
    assert all(row["verification_status"] == "UNVERIFIED" for row in payload["answers"])
    assert payload["verified"] == [], "an UNVERIFIED answer must not enter the denominator"
    manifest = resolve_generation(out).manifest
    assert manifest["algorithm_version"]
    assert manifest["generated_at_utc"]



def test_the_manifest_describes_how_the_batch_was_built(tmp_path) -> None:
    rows = _dataset(seed=121, rows=300)
    export = _write_parts(tmp_path, rows, 2)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"
    result = build_performance_streaming(
        export=export, out_dir=out, state_db=state_db, dataset="fixture",
        settlements=_settlements(rows), now=NOW,
    )
    generation = resolve_generation(out)
    assert generation.manifest is not None
    manifest = generation.manifest

    assert manifest["algorithm_version"] == result["manifest_extra"]["algorithm_version"]
    assert manifest["input_source"] == str(export.resolve())
    assert manifest["input_range"]["first_decision_utc"]
    assert manifest["input_range"]["last_decision_utc"]
    assert manifest["watermark"] == result["watermark"]
    assert manifest["coverage"] == result["coverage"]
    assert manifest["coverage"]["decisions"] == 300
    assert manifest["coverage"]["all_decision_distinct_markets"] > 0
    assert manifest["streaming"]["rows_consumed"] == 300
    assert manifest["streaming"]["parts_consumed"] == 2
    assert manifest["streaming"]["open_episodes"] >= 0
    # Every file carries the same generation id as the pointer names.
    for name in ("summary.json", "edge.json", "win-rate.json", "opportunities.json"):
        assert json.loads((generation.directory / name).read_text())["generation_id"] == manifest[
            "generation_id"
        ]


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def test_peak_memory_does_not_grow_with_the_history(tmp_path) -> None:
    """Ten times the rows, same key cardinality, must not cost ten times.

    ``tracemalloc`` measures the Python side, which is where the old builder
    died: it held every decision as a dict. The store's own buffers are
    DuckDB's, bounded by ``memory_limit`` and spilled to disk, so they do not
    show up here - which is the point of the split.
    """
    def peak(total_rows: int) -> int:
        # The *part* size is held constant, because a part is the batch and a
        # bigger batch is a bigger buffer by design. What must not grow is the
        # cost of having consumed more history at the same batch size.
        rows = _dataset(seed=1, rows=total_rows)
        export = _write_parts(tmp_path / f"e{total_rows}", rows, total_rows // 500)
        tracemalloc.start()
        with PerformanceStateStore(tmp_path / f"s{total_rows}.duckdb") as store:
            ingest_export(store, export)
        _current, high = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return high

    small = peak(2_000)
    large = peak(20_000)
    assert large < small * 2, f"{large} bytes for 10x the history of {small}"


def test_the_streaming_writer_is_byte_identical_to_the_plain_one(tmp_path) -> None:
    """Switching a payload between the two writers cannot change what is read."""
    from weatherquant.shadow.publish import (
        StreamedArray,
        atomic_write_json,
        atomic_write_json_streaming,
    )

    payload = {
        "b": [1, 2.5, None, "x"],
        "a": {"z": 1, "y": [True, {"k": "ü"}]},
        "c": 3,
    }
    plain = tmp_path / "plain.json"
    streamed = tmp_path / "streamed.json"
    atomic_write_json(plain, payload)
    atomic_write_json_streaming(streamed, payload)
    assert plain.read_bytes() == streamed.read_bytes()

    with_stream = tmp_path / "with_stream.json"
    atomic_write_json_streaming(
        with_stream,
        {"a": StreamedArray(lambda: iter([1, 2.5, None, "x"]), label="a"), "b": {"z": 1}},
    )
    assert json.loads(with_stream.read_text()) == {"a": [1, 2.5, None, "x"], "b": {"z": 1}}


def test_the_export_reader_refuses_a_directory_with_no_parts(tmp_path) -> None:
    empty = tmp_path / "export"
    (empty / "parts").mkdir(parents=True)
    with pytest.raises(PerformanceStateError, match="no published decision parts"):
        scan_export_parts(empty)


# ---------------------------------------------------------------------------
# the documented command
# ---------------------------------------------------------------------------


def test_the_documented_streaming_command_publishes_a_generation(tmp_path, capsys) -> None:
    """A directory export goes through the streaming engine, by default."""
    from weatherquant.cli import main as research_main

    rows = _dataset(seed=131, rows=400)
    export = _write_parts(tmp_path, rows, 2)
    state_db = tmp_path / "state.duckdb"
    out = tmp_path / "performance"
    settlement_path = tmp_path / "settlements.json"
    settlement_path.write_text(json.dumps(_settlements(rows)), encoding="utf-8")

    code = research_main(
        [
            "build-performance",
            "--export", str(export),
            "--out", str(out),
            "--dataset", "fixture",
            "--settlements", str(settlement_path),
            "--state-db", str(state_db),
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "consumed 400 row(s) from 2 of 2 part(s)" in printed
    assert "generation:" in printed
    assert resolve_generation(out).manifest is not None
    assert state_db.exists()

    # A second run has nothing to ingest but still republishes.
    assert research_main(
        [
            "build-performance",
            "--export", str(export),
            "--out", str(out),
            "--dataset", "fixture",
            "--state-db", str(state_db),
        ]
    ) == 0
    assert "consumed 0 row(s)" in capsys.readouterr().out


def test_the_command_refuses_to_mix_a_file_export_with_a_state_store(tmp_path, capsys) -> None:
    from weatherquant.cli import main as research_main

    rows = _dataset(seed=141, rows=50)
    export_file = tmp_path / "decisions.jsonl"
    export_file.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    code = research_main(
        [
            "build-performance",
            "--export", str(export_file),
            "--out", str(tmp_path / "out"),
            "--dataset", "fixture",
            "--state-db", str(tmp_path / "state.duckdb"),
        ]
    )
    assert code == 2
    assert "is a file, not a directory of published parts" in capsys.readouterr().err


def test_in_memory_on_a_directory_warns_before_doing_it(tmp_path, capsys) -> None:
    from weatherquant.cli import main as research_main

    rows = _dataset(seed=151, rows=120)
    export = _write_parts(tmp_path, rows, 2)
    code = research_main(
        [
            "build-performance",
            "--export", str(export),
            "--out", str(tmp_path / "out"),
            "--dataset", "fixture",
            "--in-memory",
        ]
    )
    assert code == 0
    assert "WARNING: --in-memory" in capsys.readouterr().err


def test_a_state_store_built_from_another_export_is_refused_by_the_command(
    tmp_path, capsys
) -> None:
    from weatherquant.cli import main as research_main

    rows = _dataset(seed=161, rows=200)
    state_db = tmp_path / "state.duckdb"
    _ingest(tmp_path, rows, parts=2, state_db=state_db)

    other = tmp_path / "other"
    other_export = _write_parts(other, _dataset(seed=162, rows=200), 2)
    code = research_main(
        [
            "build-performance",
            "--export", str(other_export),
            "--out", str(tmp_path / "out"),
            "--dataset", "fixture",
            "--state-db", str(state_db),
        ]
    )
    assert code == 2
    assert "does not match this run" in capsys.readouterr().err
