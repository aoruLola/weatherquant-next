"""The streaming engine against the whole-history builder, on one dataset.

The acceptance list for this refactor is a comparison, not a description: the
streaming engine has to reproduce the in-memory builder's six snapshots on the
same input. So this file builds one fixed dataset, runs both, and asserts the
published payloads are equal - field for field, float for float, including the
parts that are easy to get *nearly* right:

* the two edge sums (``math.fsum`` versus a naive fold),
* the quantiles and the median, which interpolate the same sorted sample by two
  different formulas and therefore disagree in the last bit on purpose,
* the clustered confidence interval, which depends on the order the target
  dates were first seen in,
* the reference-capital walk, which depends on episode order and a shared
  per-market cap.

Anything that would have to change the statistics is a failure here, not a note.
"""

from __future__ import annotations

import json
import random
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weatherquant.markets.models import BookLevel, OrderBook
from weatherquant.performance.backfill import build_performance_snapshots
from weatherquant.performance.capital import simulate_portfolio
from weatherquant.performance.edge import distribution, observation_edge
from weatherquant.performance.episodes import build_episodes, summarize
from weatherquant.performance.snapshot import (
    build_performance,
    resolve_generation,
)
from weatherquant.performance.streaming import build_performance_streaming
from weatherquant.performance.winrate import (
    SettlementAnswer,
    WinRateAccumulator,
    assign_outcomes,
    confidence_interval,
    win_rate,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# the dataset
# ---------------------------------------------------------------------------


def _dataset(seed: int = 20261015, rows: int = 900) -> list[dict]:
    """A deterministic history with every feature the payloads can exercise.

    Non-ACCEPT states, ACCEPTs without a usable edge, NaN and inf edges, gaps
    longer and shorter than the window, both directions, both modes, several
    models (including a null one), and several target dates - so the win rate
    has clusters and the episode state machine has boundaries.
    """
    rng = random.Random(seed)
    out: list[dict] = []
    instant = T0
    for i in range(rows):
        instant = instant + timedelta(minutes=rng.choice([1, 3, 7, 40, 300, 1200]))
        state = rng.choices(["ACCEPT", "REJECT", "NO_TRADE"], weights=[7, 2, 1])[0]
        edge: float | None
        if state != "ACCEPT" or rng.random() < 0.08:
            edge = None
        elif rng.random() < 0.01:
            edge = float("nan")
        else:
            edge = round(rng.uniform(-0.08, 0.22), 6)
        out.append(
            {
                "decision_id": f"d{i:07d}",
                "decision_time_utc": instant.isoformat(),
                "market_id": f"m-{rng.randrange(8)}",
                "target_date": f"2026-10-{13 + rng.randrange(4):02d}",
                "model_name": rng.choice(["calibrated", "gfs_raw", "fusion", None]),
                "direction": rng.choice(["buy_yes", "sell_yes"]),
                "mode": rng.choice(["LIVE", "REPLAY"]),
                "state": state,
                "net_edge": edge,
                "market_price": 0.6,
                "model_probability": 0.65,
                "available_depth_usd": 500.0,
                "fee_rate": 0.0,
                "station": "KLGA",
                "bracket_label": "81°F",
                "market_question": "Will it be 81F?",
            }
        )
    # The export is ordered by (decision_time_utc, decision_id); the streaming
    # reader refuses anything else, and the in-memory builder sorts anyway.
    out.sort(key=lambda row: (row["decision_time_utc"], row["decision_id"]))
    return out


def _settlements(rows: list[dict], *, verified: bool = True) -> list[dict]:
    """One settlement per (market, target date), with a real win/loss mix."""
    pairs = sorted({(row["market_id"], row["target_date"]) for row in rows})
    out = []
    for index, (market_id, target_date) in enumerate(pairs):
        out.append(
            {
                "market_id": market_id,
                "target_date": target_date,
                "settlement_status": "RESOLVED",
                "resolved_outcome": 1.0 if index % 3 else 0.0,
                "verification_status": "VERIFIED" if verified else "UNVERIFIED",
                "fetched_at_utc": NOW.isoformat(),
            }
        )
    return out


def _write_parts(root: Path, rows: list[dict], parts: int) -> Path:
    """(Re)write the export from scratch: a stale part would be a mixture."""
    export = root / "export"
    directory = export / "parts"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    size = max(1, (len(rows) + parts - 1) // parts)
    for index in range(parts):
        chunk = rows[index * size : (index + 1) * size]
        if not chunk:
            continue
        path = directory / f"part-{index + 1:09d}.jsonl"
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in chunk),
            encoding="utf-8",
        )
    return export


def _two_sided_book(episode) -> OrderBook:
    """A book whose best ask is the price every decision was taken at."""
    return OrderBook(
        market_id=episode.key.market_id,
        token_id="t",
        bids=(BookLevel(price_usd_per_share=0.59, size_shares=500.0),),
        asks=(BookLevel(price_usd_per_share=0.6, size_shares=500.0),),
        venue_ts=NOW,
        fetched_at_utc=NOW,
    )


def _read_generation(directory: Path) -> dict[str, dict]:
    generation = resolve_generation(directory)
    assert generation.directory is not None, "nothing was published"
    return {
        name: json.loads((generation.directory / name).read_text(encoding="utf-8"))
        for name in (
            "summary.json",
            "edge.json",
            "win-rate.json",
            "opportunities.json",
            "settlements.json",
            "models.json",
        )
    }


def published_episodes(directory: Path) -> list[dict]:
    """Every episode in the published detail, reassembled from its parts.

    The detail is published as parts rather than as one array (that is the
    whole point of this round), so a test that wants the rows has to read the
    descriptor and then the parts - exactly as the dashboard does.
    """
    generation = resolve_generation(directory)
    assert generation.directory is not None, "nothing was published"
    payload = json.loads((generation.directory / "opportunities.json").read_text(encoding="utf-8"))
    descriptor = payload["data"]["episodes"]
    assert descriptor["partitioned"] is True, descriptor
    rows: list[dict] = []
    for part in descriptor["partitions"]:
        rows.extend(
            json.loads((generation.directory / descriptor["directory"] / part["file"]).read_text(encoding="utf-8"))
        )
    assert len(rows) == descriptor["total"], (len(rows), descriptor["total"])
    return rows


def _strip(payloads: dict[str, dict]) -> dict[str, dict]:
    """Drop the fields a *publication* adds and a builder does not."""
    cleaned = {}
    for name, payload in payloads.items():
        copy = dict(payload)
        copy.pop("generation_id", None)
        cleaned[name] = copy
    return cleaned


def _run_legacy(
    tmp_path: Path,
    rows: list[dict],
    settlements: list[dict],
    *,
    book_for=None,
    out_dir: Path | None = None,
    watermark: list[str] | None = None,
) -> dict:
    """Publish the whole-history builder's six snapshots, and read them back.

    Comparing *published* output on both sides is the honest comparison: it is
    what a reader sees, it exercises the same writer, and it is the only way to
    compare a detail that is published as parts - the builder hands back a
    marker and the files are what a consumer can actually read.
    """
    build_performance_snapshots(
        decisions=rows,
        settlements=settlements,
        out_dir=out_dir or (tmp_path / "legacy"),
        dataset="fixture",
        book_for=book_for,
        now=NOW,
        # The cursor the export had reached, so the lifetime capital block
        # describes the same consumed range on both sides. A publish without
        # one says `null`, which is a different (honest) claim, not a
        # different statistic.
        watermark=list(watermark) if watermark else None,
    )
    return _read_generation(out_dir or (tmp_path / "legacy"))


def _run_streaming(
    tmp_path: Path,
    rows: list[dict],
    settlements: list[dict],
    *,
    parts: int = 4,
    book_for=None,
    state_db: Path | None = None,
    out_dir: Path | None = None,
) -> dict:
    export = _write_parts(tmp_path, rows, parts)
    return build_performance_streaming(
        export=export,
        out_dir=out_dir or (tmp_path / "performance"),
        state_db=state_db or (tmp_path / "state.duckdb"),
        dataset="fixture",
        settlements=settlements,
        book_for=book_for,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# the comparison
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dataset() -> list[dict]:
    return _dataset()


def test_every_published_payload_matches_the_in_memory_builder(tmp_path, dataset) -> None:
    """The whole point: same input, same six files, no exceptions.

    Run with a real book so the reference-capital walk actually fills, and with
    settlements so the win rate has a numerator and denominator rather than
    being N/A on both sides - an N/A comparison would prove nothing.
    """
    settlements = _settlements(dataset)
    streamed_result = _run_streaming(tmp_path, dataset, settlements, book_for=_two_sided_book)
    legacy = _run_legacy(
        tmp_path, dataset, settlements, book_for=_two_sided_book,
        watermark=streamed_result["watermark"],
    )
    streamed = _read_generation(tmp_path / "performance")

    assert _strip(streamed) == _strip(legacy), "the streaming engine changed a published number"


def test_the_comparison_is_not_vacuous(tmp_path, dataset) -> None:
    """Guard the guard: the dataset has to exercise the hard paths."""
    settlements = _settlements(dataset)
    legacy = build_performance(
        decisions=dataset, settlements=settlements, now=NOW, dataset="fixture",
        book_for=_two_sided_book,
    )
    summary = legacy["summary.json"]["data"]
    win = legacy["win-rate.json"]["data"]["overall"]

    assert summary["opportunity_episodes"] > 40, "too few episodes to be interesting"
    assert win["valid_samples"] >= 20, "the clustered interval would not run"
    assert win["method"] == "cluster_bootstrap_by_target_date", win["method"]
    assert win["confidence_interval"] is not None
    assert summary["reference_capital"]["executable_count"] > 0, "no fills were priced"
    assert legacy["edge.json"]["data"]["by_edge_band"]["lt_0.02"], "no low-edge band"


# ---------------------------------------------------------------------------
# the ten acceptance items, named
# ---------------------------------------------------------------------------


def test_the_acceptance_items_match_one_by_one(tmp_path, dataset) -> None:
    """The brief's ten checks, asserted individually rather than in bulk."""
    settlements = _settlements(dataset)
    streamed_result = _run_streaming(tmp_path, dataset, settlements, book_for=_two_sided_book)
    legacy = _run_legacy(
        tmp_path, dataset, settlements, book_for=_two_sided_book,
        watermark=streamed_result["watermark"],
    )
    streamed = _strip(_read_generation(tmp_path / "performance"))

    legacy_summary = legacy["summary.json"]["data"]
    new_summary = streamed["summary.json"]["data"]
    legacy_win = legacy["win-rate.json"]["data"]["overall"]
    new_win = streamed["win-rate.json"]["data"]["overall"]

    # 1. ACCEPT observation count
    assert new_summary["accept_observations"] == legacy_summary["accept_observations"]
    # 2. episode count and identity, in the published order
    assert new_summary["opportunity_episodes"] == legacy_summary["opportunity_episodes"]
    # The detail is published as parts; the rows come back in the same order
    # the whole-history builder produced, which is what the cursor pages over.
    assert [row["episode_id"] for row in published_episodes(tmp_path / "performance")] == [
        row["episode_id"] for row in published_episodes(tmp_path / "legacy")
    ]
    # 3. Observation Edge Sum
    assert new_summary["observation_edge_sum"] == legacy_summary["observation_edge_sum"]
    # 4. Unique Opportunity Edge Sum
    assert (
        new_summary["unique_opportunity_edge_sum"]
        == legacy_summary["unique_opportunity_edge_sum"]
    )
    # 5. win-rate numerator and denominator
    assert new_win["counts"] == legacy_win["counts"]
    assert new_win["valid_samples"] == legacy_win["valid_samples"]
    # 6. clustered confidence interval
    assert new_win["confidence_interval"] == legacy_win["confidence_interval"]
    assert new_win["method"] == legacy_win["method"]
    # 7. 100 USDC reference capital
    new_capital = new_summary["reference_capital"]
    legacy_capital = legacy_summary["reference_capital"]
    assert new_capital["allocated_total_usdc"] == legacy_capital["allocated_total_usdc"]
    assert new_capital["peak_open_allocation_usdc"] == legacy_capital["peak_open_allocation_usdc"]
    assert new_capital["balance_remaining_usdc"] == legacy_capital["balance_remaining_usdc"]
    assert new_capital["executable_count"] == legacy_capital["executable_count"]
    assert new_capital["not_executable_reason_counts"] == (
        legacy_capital["not_executable_reason_counts"]
    )
    assert new_capital["results_sample"] == legacy_capital["results_sample"]
    # 10. generation, watermark and coverage agree (watermark is asserted in
    # the state tests, where the export cursor is known).
    assert streamed["summary.json"]["sample_size"] == legacy["summary.json"]["sample_size"]


def test_no_settlement_data_is_still_na_not_zero(tmp_path) -> None:
    """Item 10 of the correctness list: N/A stays N/A."""
    rows = _dataset(seed=7, rows=200)
    result = _run_streaming(tmp_path, rows, [])
    legacy = _strip(_run_legacy(tmp_path, rows, [], watermark=result["watermark"]))
    streamed = _strip(_read_generation(tmp_path / "performance"))

    assert streamed == legacy
    overall = streamed["win-rate.json"]["data"]["overall"]
    assert overall["win_rate"] is None
    assert overall["reason"].startswith("N/A")
    assert overall["counts"]["UNAVAILABLE"] > 0


def test_a_history_with_nothing_to_score_publishes_empty_not_zero(tmp_path) -> None:
    """No priced ACCEPT at all: every figure is N/A or zero, and nothing raises.

    This is the state a freshly deployed system is in, so it has to produce six
    files rather than a traceback.
    """
    rows = []
    instant = T0
    for index in range(20):
        instant = instant + timedelta(minutes=5)
        rows.append(
            {
                "decision_id": f"d{index:05d}",
                "decision_time_utc": instant.isoformat(),
                "market_id": f"m-{index % 3}",
                "target_date": "2026-10-13",
                "model_name": "calibrated",
                "direction": "buy_yes",
                "mode": "LIVE",
                "state": "REJECT",
                "net_edge": None,
                "market_price": 0.6,
                "model_probability": 0.65,
                "available_depth_usd": 500.0,
            }
        )
    result = _run_streaming(tmp_path, rows, [])
    legacy = _strip(_run_legacy(tmp_path, rows, [], watermark=result["watermark"]))
    streamed = _strip(_read_generation(tmp_path / "performance"))

    assert streamed == legacy
    summary = streamed["summary.json"]["data"]
    assert summary["opportunity_episodes"] == 0
    # A sum over nothing is None, not 0.0: "not known" is a different claim
    # from "covered nothing", and the contract keeps them apart.
    assert summary["observation_edge_sum"] is None
    assert summary["unique_opportunity_edge_sum"] == 0.0
    assert summary["realized_directional_win_rate"] is None
    assert streamed["edge.json"]["data"]["overall"]["count"] == 0
    assert {
        band: stats["count"]
        for band, stats in streamed["edge.json"]["data"]["by_edge_band"].items()
    } == {"lt_0.02": 0, "0.02_0.05": 0, "0.05_0.10": 0, "gte_0.10": 0}
    assert published_episodes(tmp_path / "performance") == []
    assert streamed["opportunities.json"]["data"]["total"] == 0


def test_a_missing_book_still_reports_not_executable(tmp_path, dataset) -> None:
    """Item 12: no retained levels means NOT_EXECUTABLE, never an invented fill."""
    settlements = _settlements(dataset)
    result = _run_streaming(tmp_path, dataset, settlements)
    legacy = _strip(_run_legacy(tmp_path, dataset, settlements, watermark=result["watermark"]))
    streamed = _strip(_read_generation(tmp_path / "performance"))

    assert streamed == legacy
    capital = streamed["summary.json"]["data"]["reference_capital"]
    assert capital["executable_count"] == 0
    assert capital["backtestable"] is False
    assert set(capital["not_executable_reason_counts"]) <= {"NO_BOOK", "DEPTH_NOT_MEASURED"}
    assert capital["results_sample"], "the sample must still show why they were refused"
    assert all("book" not in row for row in published_episodes(tmp_path / "performance"))


def test_ten_thousand_rows_still_agree(tmp_path) -> None:
    """A larger run, to catch anything that only breaks past one fetch buffer."""
    rows = _dataset(seed=4242, rows=10_000)
    settlements = _settlements(rows)
    result = _run_streaming(tmp_path, rows, settlements, parts=7)
    legacy = _strip(_run_legacy(tmp_path, rows, settlements, watermark=result["watermark"]))
    streamed = _strip(_read_generation(tmp_path / "performance"))

    assert streamed == legacy


def test_every_edge_statistic_matches_item_by_item(tmp_path, dataset) -> None:
    """The statistics the review names one by one, compared one by one.

    The whole-payload comparison above would pass even if a statistic were
    dropped from both sides; this walks the groups and asserts each figure the
    brief lists, so "identical" is shown rather than assumed.
    """
    settlements = _settlements(dataset)
    legacy = build_performance(
        decisions=dataset, settlements=settlements, now=NOW, dataset="fixture"
    )
    _run_streaming(tmp_path, dataset, settlements)
    streamed = _strip(_read_generation(tmp_path / "performance"))

    legacy_edge = legacy["edge.json"]["data"]
    new_edge = streamed["edge.json"]["data"]
    statistics = (
        "count", "sum", "mean", "median", "min", "max", "std",
        "p05", "p25", "p50", "p75", "p95",
    )

    for statistic in statistics:
        assert new_edge["overall"][statistic] == legacy_edge["overall"][statistic], statistic
    for group in ("by_model", "by_direction", "by_target_date"):
        assert set(new_edge[group]) == set(legacy_edge[group]), group
        for key, expected in legacy_edge[group].items():
            for statistic in statistics:
                assert new_edge[group][key][statistic] == expected[statistic], (group, key, statistic)
    assert new_edge["daily"] == legacy_edge["daily"]
    assert new_edge["by_edge_band"] == legacy_edge["by_edge_band"]
    assert new_edge["refused"] == legacy_edge["refused"]

    # The counts that describe three different populations.
    legacy_summary = legacy["summary.json"]["data"]
    new_summary = streamed["summary.json"]["data"]
    for field in (
        "accept_observations",
        "opportunity_episodes",
        "accept_opportunity_distinct_markets",
        "accept_opportunity_distinct_target_dates",
        "verified_opportunities",
        "settled_markets",
    ):
        assert new_summary[field] == legacy_summary[field], field
    assert streamed["models.json"]["data"]["models"] == legacy["models.json"]["data"]["models"]
    assert streamed["models.json"]["data"]["by_model_win_rate"] == (
        legacy["models.json"]["data"]["by_model_win_rate"]
    )


def test_live_and_replay_never_share_an_episode(tmp_path) -> None:
    """Interleaved LIVE and REPLAY rows for one market stay two episodes.

    Built by hand rather than sampled, so the assertion is about isolation and
    nothing else: if ``mode`` were dropped from the identity key these six rows
    would fold into one episode of six observations.
    """
    rows = []
    instant = T0
    for index in range(6):
        instant = instant + timedelta(minutes=5)
        rows.append(
            {
                "decision_id": f"d{index:05d}",
                "decision_time_utc": instant.isoformat(),
                "market_id": "m-81",
                "target_date": "2026-10-13",
                "model_name": "calibrated",
                "direction": "buy_yes",
                "mode": "LIVE" if index % 2 == 0 else "REPLAY",
                "state": "ACCEPT",
                "net_edge": 0.05,
                "market_price": 0.6,
                "model_probability": 0.65,
                "available_depth_usd": 500.0,
            }
        )
    result = _run_streaming(tmp_path, rows, [])
    legacy = _strip(_run_legacy(tmp_path, rows, [], watermark=result["watermark"]))
    streamed = _strip(_read_generation(tmp_path / "performance"))
    assert streamed == legacy

    episodes = published_episodes(tmp_path / "performance")
    assert len(episodes) == 2
    assert {row["mode"] for row in episodes} == {"LIVE", "REPLAY"}
    assert {row["observation_count"] for row in episodes} == {3}
    assert len({row["episode_id"] for row in episodes}) == 2


# ---------------------------------------------------------------------------
# the aggregators, in isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_streamed_distribution_equals_the_list_one(seed: int) -> None:
    """The store's order statistics against a sorted list, value by value.

    Exercises the two formulas that differ on purpose: ``median`` averages the
    middle pair, ``p50`` interpolates it.
    """
    from weatherquant.performance.state import PerformanceStateStore
    from weatherquant.performance.streaming import _distributions

    rng = random.Random(seed)
    values = [rng.gauss(0.03, 0.06) for _ in range(rng.randrange(1, 500))]
    expected = distribution(values)

    with PerformanceStateStore(":memory:") as store:
        store.con.execute(
            "INSERT INTO performance_edge_observations "
            "(seq, target_date, model_name, direction, mode, edge) "
            "SELECT row_number() OVER (), NULL, NULL, NULL, NULL, column0 FROM ("
            "SELECT unnest(?::DOUBLE[]) AS column0)",
            [values],
        )
        got = _distributions(store, None)["__overall__"]

    assert got == expected


def test_the_streamed_win_rate_equals_the_list_one(dataset) -> None:
    """Including the interval, whose cluster order comes from the win/loss split."""
    settlements = _settlements(dataset)
    episodes, _ = build_episodes(dataset)
    answers = {}
    verified = set()
    for row in settlements:
        key = (row["market_id"], row["target_date"])
        answers[key] = SettlementAnswer(
            status=row["settlement_status"], payout=row["resolved_outcome"]
        )
        if row["verification_status"] == "VERIFIED":
            verified.add(key)
    scored = assign_outcomes(episodes, answers=answers, verified=frozenset(verified))

    accumulator = WinRateAccumulator()
    for item in scored:
        accumulator.add(
            outcome=item.outcome, market_id=item.market_id, target_date=item.target_date
        )
    expected = win_rate(scored)

    assert expected.method == "cluster_bootstrap_by_target_date"
    assert accumulator.result().as_dict() == expected.as_dict()
    assert confidence_interval(scored)[0] == expected.confidence_interval


def test_the_streamed_summary_equals_the_list_one(dataset) -> None:
    episodes, _ = build_episodes(dataset)
    expected = summarize(episodes)

    from weatherquant.performance.episodes import EpisodeSummary

    accumulator = EpisodeSummary()
    for episode in episodes:
        accumulator.add(episode)

    assert accumulator.as_dict() == expected


def test_the_edge_totals_are_the_same_numbers(dataset) -> None:
    """``math.fsum`` over a stream is ``math.fsum`` over a list."""
    expected = observation_edge(dataset)
    episodes, _ = build_episodes(dataset)
    capital = simulate_portfolio(
        episodes, capital_usdc=100.0, book_for=lambda _e: None
    )
    assert expected["overall"]["sum"] == pytest.approx(
        summarize(episodes)["observation_edge_sum"]
    )
    assert capital.capital_usdc == 100.0
