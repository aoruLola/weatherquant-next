"""API contract tests: envelopes, freshness, filters, degradation, safety.

Mapped to the acceptance list in the task spec (§11 backend):

* schema / empty state / corrupt JSON / staleness
* collector disconnect, shadow REPLAY, LIVE-REPLAY separation
* pagination, filters, numeric units
* no production DuckDB access, read-only over the data tree, no path leakage
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from helpers import (
    build_tree,
    collector_heartbeat,
    envelope,
    gamma_event,
    rest_book,
    shadow_heartbeat,
    shadow_report,
    utcnow,
    write_json,
    write_jsonl,
)

from weatherquant.dashboard.api import create_app
from weatherquant.dashboard.contracts import FreshnessState

ALL_ENDPOINTS = [
    "/api/v1/overview",
    "/api/v1/collector/health",
    "/api/v1/collector/metrics",
    "/api/v1/shadow/health",
    "/api/v1/shadow/report",
    "/api/v1/markets",
    "/api/v1/forecasts",
    "/api/v1/decisions",
    "/api/v1/system",
]


def assert_envelope(body: dict, *, api: str, dataset: str = "live") -> None:
    assert body["schema_version"] == 1
    assert body["api"] == api
    assert body["dataset"] == dataset
    assert isinstance(body["generated_at_utc"], str)
    assert body["freshness"]["state"] in {s.value for s in FreshnessState}


# ---------------------------------------------------------------------------
# envelope + schema
# ---------------------------------------------------------------------------


def test_every_endpoint_returns_the_versioned_envelope(client: TestClient) -> None:
    apis = {
        "/api/v1/overview": "overview",
        "/api/v1/collector/health": "collector-health",
        "/api/v1/collector/metrics": "collector-metrics",
        "/api/v1/shadow/health": "shadow-health",
        "/api/v1/shadow/report": "shadow-report",
        "/api/v1/markets": "markets",
        "/api/v1/forecasts": "forecasts",
        "/api/v1/decisions": "decisions",
        "/api/v1/system": "system",
    }
    for url, api in apis.items():
        response = client.get(url)
        assert response.status_code == 200, f"{url}: {response.text[:300]}"
        assert_envelope(response.json(), api=api)


def test_healthz_reports_sources_without_claiming_them(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["data"]["status"] == "ok"
    assert body["schema_version"] == 1
    assert body["api"] == "healthz"
    # Liveness of the dashboard is not health of the sources: both are present.
    assert set(body["data"]["sources"]) == {
        "collector_heartbeat",
        "shadow_heartbeat",
        "shadow_report",
    }
    # the envelope's freshness reflects the sources behind it
    assert body["freshness"]["state"] in {s.value for s in FreshnessState}


def test_unknown_api_path_is_404_not_index(client: TestClient) -> None:
    assert client.get("/api/v1/nope").status_code == 404


# ---------------------------------------------------------------------------
# empty / missing / corrupt
# ---------------------------------------------------------------------------


def test_empty_tree_is_a_legal_empty_state_not_an_error(tmp_path: Path, settings_for) -> None:
    app = create_app(settings_for(tmp_path))  # nothing built under it
    with TestClient(app) as client:
        for url in ALL_ENDPOINTS:
            response = client.get(url)
            assert response.status_code == 200, f"{url} -> {response.status_code}"
            body = response.json()
            assert body["freshness"]["state"] == "missing", url
        overview = client.get("/api/v1/overview").json()["data"]
        assert overview["collector"]["present"] is False
        assert overview["collector"]["reason"]
        markets = client.get("/api/v1/markets").json()["data"]
        assert markets["items"] == []
        assert markets["total"] == 0
        decisions = client.get("/api/v1/decisions").json()["data"]
        assert decisions["rows"] == []
        assert decisions["rows_status"] == "unavailable"


def test_corrupt_json_is_reported_not_swallowed(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, collector=False, shadow=False, report=False, archive=False)
    (root / "collector" / "health").mkdir(parents=True, exist_ok=True)
    (root / "collector" / "health" / "heartbeat.json").write_text("{not json", encoding="utf-8")
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        body = client.get("/api/v1/collector/health").json()
        assert body["freshness"]["state"] == "corrupt"
        assert "collector-heartbeat" in body["freshness"]["reason"]
        assert body["data"]["present"] is False
        # a corrupt file must not 500 the rest of the dashboard
        assert client.get("/api/v1/system").status_code == 200


def test_half_written_snapshot_reads_as_corrupt_not_as_data(tmp_path: Path, settings_for) -> None:
    """A truncated document (the failure mode of a non-atomic writer) must
    never be interpreted as an empty-but-valid snapshot."""
    root = build_tree(tmp_path, report=True)
    report_path = root / "shadow" / "reports" / "report.json"
    full = report_path.read_text(encoding="utf-8")
    report_path.write_text(full[: len(full) // 2], encoding="utf-8")
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        body = client.get("/api/v1/shadow/report").json()
        assert body["freshness"]["state"] == "corrupt"
        assert body["data"]["present"] is False


def test_wrong_json_type_is_corrupt(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, shadow=False, report=False, archive=False)
    (root / "shadow" / "health").mkdir(parents=True, exist_ok=True)
    (root / "shadow" / "health" / "heartbeat.json").write_text("[1, 2, 3]", encoding="utf-8")
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        body = client.get("/api/v1/shadow/health").json()
        assert body["freshness"]["state"] == "corrupt"


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------


def test_stale_heartbeat_reports_age_and_state(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, collector_age_seconds=600.0)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        body = client.get("/api/v1/collector/health").json()
        freshness = body["freshness"]
        assert freshness["state"] == "stale"
        assert freshness["age_seconds"] == pytest.approx(600.0, abs=5.0)
        assert freshness["stale_after_seconds"] == 120.0


def test_fresh_heartbeat_is_fresh(client: TestClient) -> None:
    body = client.get("/api/v1/collector/health").json()
    assert body["freshness"]["state"] == "fresh"
    assert body["freshness"]["age_seconds"] < 120.0
    assert body["data"]["health_status"] == "healthy"  # the probe's own verdict


def test_report_staleness_honours_its_own_window(tmp_path: Path, settings_for) -> None:
    now = utcnow()
    root = build_tree(tmp_path, report=shadow_report(now=now, age_seconds=100.0))
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        fresh_body = client.get("/api/v1/shadow/report").json()
        assert fresh_body["freshness"]["state"] == "fresh"

    root2 = build_tree(tmp_path / "old", report=shadow_report(now=now, age_seconds=900.0))
    app2 = create_app(settings_for(root2))
    with TestClient(app2) as client:
        stale_body = client.get("/api/v1/shadow/report").json()
        assert stale_body["freshness"]["state"] == "stale"


# ---------------------------------------------------------------------------
# disconnection / REPLAY
# ---------------------------------------------------------------------------


def test_collector_disconnect_is_visible_and_never_green(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, collector=None)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        assert client.get("/healthz").json()["data"]["sources"]["collector_heartbeat"] == "missing"
        health = client.get("/api/v1/collector/health").json()
        assert health["data"]["present"] is False
        assert health["freshness"]["state"] == "missing"
        overview = client.get("/api/v1/overview").json()
        assert overview["freshness"]["state"] == "missing"


def test_shadow_replay_mode_is_surfaced_everywhere(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, shadow_mode="REPLAY", report_mode="REPLAY")
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        health = client.get("/api/v1/shadow/health").json()["data"]
        assert health["mode"] == "REPLAY"
        decisions = client.get("/api/v1/decisions").json()["data"]
        totals = decisions["totals"]
        assert totals["live_accepted"] == 0
        assert totals["replay_accepted"] == 3
        # replay accepts must stay split from live ones, never merged
        assert totals["by_mode"].keys() == {"LIVE", "REPLAY"}
        report = client.get("/api/v1/shadow/report").json()["data"]
        assert any("REPLAY" in w for w in report["warnings"])


def test_live_and_replay_never_pool_in_the_default_totals(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, report_mode="LIVE")
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        totals = client.get("/api/v1/decisions").json()["data"]["totals"]
        assert totals["replay_accepted"] == 0
        assert totals["live_accepted"] == 3


# ---------------------------------------------------------------------------
# markets: filters, pagination, units
# ---------------------------------------------------------------------------


def test_markets_pagination_and_filters(client: TestClient) -> None:
    first = client.get("/api/v1/markets?limit=1").json()["data"]
    assert first["total"] == 2
    assert len(first["items"]) == 1
    assert first["next_cursor"] == "1"

    second = client.get("/api/v1/markets?limit=1&cursor=1").json()["data"]
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert {first["items"][0]["market_id"], second["items"][0]["market_id"]} == {"m-81", "m-low"}

    only_81 = client.get("/api/v1/markets?q=81").json()["data"]
    assert only_81["total"] == 1
    assert only_81["items"][0]["market_id"] == "m-81"

    tradeable = client.get("/api/v1/markets?status=tradeable").json()["data"]
    assert tradeable["total"] == 2

    target_date = tradeable["items"][0]["target_date"]
    by_date = client.get(f"/api/v1/markets?date={target_date}").json()["data"]
    assert by_date["total"] == 2

    missing_date = client.get("/api/v1/markets?date=2001-01-01").json()["data"]
    assert missing_date["total"] == 0
    assert missing_date["items"] == []


def test_market_quotes_have_units_and_staleness(client: TestClient) -> None:
    items = client.get("/api/v1/markets?q=81").json()["data"]["items"]
    market = items[0]
    book = market["book"]
    assert book["available"] is True
    assert book["best_bid"] == pytest.approx(0.62)
    assert book["best_ask"] == pytest.approx(0.65)
    assert book["spread"] == pytest.approx(0.03)
    # USD notionals: bids 0.62*100 + 0.61*50 = 92.5, asks 0.65*80 + 0.66*40 = 78.4
    assert book["bid_depth_usd"] == pytest.approx(92.5, abs=1e-6)
    assert book["ask_depth_usd"] == pytest.approx(78.4, abs=1e-6)
    assert book["stale"] is False
    assert book["age_seconds"] < 60
    # model fields are honestly absent, with the integration pointer attached
    assert market["model_probability"] is None
    assert market["model_source"] == "unavailable"
    assert "data-contract" in market["model_reason"]


def test_stale_book_is_marked_stale_not_quoted_as_live(tmp_path: Path, settings_for) -> None:
    now = utcnow()
    root = build_tree(tmp_path, archive=False)
    old = now - timedelta(seconds=3600)
    gamma = gamma_event(now=old, target=str((now + timedelta(days=1)).date()))
    (root / "collector" / "raw").mkdir(parents=True, exist_ok=True)
    write_jsonl(
        root / "collector" / "raw" / "gamma" / f"{now.date().isoformat()}.jsonl",
        [envelope("gamma", gamma, now=old)],
    )
    write_jsonl(
        root / "collector" / "raw" / "rest" / f"{now.date().isoformat()}.jsonl",
        [envelope("rest", rest_book("tok-81-yes", now=old), now=old, seq=2)],
    )
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        body = client.get("/api/v1/markets?q=81").json()["data"]
        assert body["source_status"] == "stale"
        book = body["items"][0]["book"]
        assert book["stale"] is True
        assert book["age_seconds"] > 900
        assert "old" in book["reason"]
        # stale books are still filterable as such
        stale_only = client.get("/api/v1/markets?book=stale").json()["data"]
        assert stale_only["total"] >= 1


def test_market_decision_filter_needs_the_producer(tmp_path: Path, settings_for) -> None:
    """Markets whose LATEST decision is X - honestly empty without a producer."""
    root = build_tree(tmp_path, producer_snapshots=True)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        accepted = client.get("/api/v1/markets?decision=ACCEPT").json()["data"]
        assert accepted["total"] == 1
        assert accepted["items"][0]["market_id"] == "m-81"
        assert accepted["filters_applied"]["decision"] == "ACCEPT"

        rejected = client.get("/api/v1/markets?decision=REJECT").json()["data"]
        assert rejected["total"] == 0  # the only producer row says ACCEPT

    # without the snapshot nothing has a decision at all
    root2 = build_tree(tmp_path / "bare", producer_snapshots=False)
    app2 = create_app(settings_for(root2))
    with TestClient(app2) as client:
        none = client.get("/api/v1/markets?decision=ACCEPT").json()["data"]
        assert none["total"] == 0


def test_decisions_since_filters_the_row_window(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, producer_snapshots=True)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        everything = client.get("/api/v1/decisions").json()["data"]
        assert everything["total_rows"] == 4

        # fixture rows are 0s/30s/60s/90s old: a 45s window keeps two.
        # URL-encoded: a raw '+' in a query string decodes to a space.
        since = quote((utcnow() - timedelta(seconds=45)).isoformat())
        windowed = client.get(f"/api/v1/decisions?since={since}").json()["data"]
        assert windowed["total_rows"] == 2
        assert windowed["filters_applied"]["since"] is not None

        # far future window: everything drops, but it is still a legal state
        future = quote((utcnow() + timedelta(hours=1)).isoformat())
        empty = client.get(f"/api/v1/decisions?since={future}").json()["data"]
        assert empty["total_rows"] == 0
        assert empty["rows"] == []


def test_invalid_since_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/decisions?since=not-a-date").status_code == 422


def test_market_detail_and_unknown_market(client: TestClient) -> None:
    detail = client.get("/api/v1/markets/m-81").json()["data"]
    assert detail["market"]["market_id"] == "m-81"
    assert detail["depth"]["bid_depth_usd"] == pytest.approx(92.5, abs=1e-6)
    assert detail["recent_decisions_status"] == "unavailable"

    unknown = client.get("/api/v1/markets/does-not-exist").json()["data"]
    assert unknown["market"] is None
    assert "not found" in unknown["reason"]


# ---------------------------------------------------------------------------
# producer snapshots (contract consumers)
# ---------------------------------------------------------------------------


def test_producer_snapshots_fill_the_detail_views(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, producer_snapshots=True)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        markets = client.get("/api/v1/markets").json()["data"]
        assert markets["source"] == "market-snapshot"
        top = markets["items"][0]
        assert top["model_probability"] == pytest.approx(0.41)
        assert top["model_source"] == "market-snapshot"
        assert top["book"]["best_bid"] == pytest.approx(0.62)

        decisions = client.get("/api/v1/decisions").json()["data"]
        assert decisions["rows_status"] == "fresh"
        assert decisions["total_rows"] == 4
        assert [row["state"] for row in decisions["rows"]] == ["ACCEPT", "REJECT", "REJECT", "NO_TRADE"]

        # filters apply to the row window
        accepts = client.get("/api/v1/decisions?state=ACCEPT").json()["data"]
        assert accepts["total_rows"] == 1
        replay = client.get("/api/v1/decisions?mode=REPLAY").json()["data"]
        assert replay["total_rows"] == 0
        by_market = client.get("/api/v1/decisions?market_id=m-81").json()["data"]
        assert by_market["total_rows"] == 4

        forecasts = client.get("/api/v1/forecasts").json()["data"]
        assert forecasts["distributions_status"] in ("fresh", "stale")
        assert forecasts["distributions"][0]["model"] == "calibrated"
        probabilities = [b["probability"] for b in forecasts["distributions"][0]["buckets"]]
        assert sum(probabilities) == pytest.approx(1.0)

        # overview model-vs-market now has a real row
        overview = client.get("/api/v1/overview").json()["data"]
        assert overview["model_vs_market_status"] != "unavailable"
        assert overview["model_vs_market"][0]["model_probability"] == pytest.approx(0.41)


def test_missing_producer_is_unavailable_with_an_integration_pointer(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, producer_snapshots=False)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        decisions = client.get("/api/v1/decisions").json()["data"]
        assert decisions["rows_status"] == "unavailable"
        assert "data-contract" in decisions["rows_reason"]
        forecasts = client.get("/api/v1/forecasts").json()["data"]
        assert forecasts["distributions_status"] == "unavailable"
        assert "data-contract" in forecasts["distributions_reason"]
        # aggregates from the report are real even while rows are not
        assert decisions["totals_status"] == "fresh"
        assert decisions["totals"]["accepted"] == 3


# ---------------------------------------------------------------------------
# forecasts / system / overview
# ---------------------------------------------------------------------------


def test_forecasts_lists_models_and_never_fabricates_observations(client: TestClient) -> None:
    data = client.get("/api/v1/forecasts").json()["data"]
    names = {m["name"] for m in data["models"]}
    assert {"calibrated", "gfs_raw"} <= names
    assert data["station"] == "KLGA"
    assert len(data["target_dates"]) == 2
    assert data["observation_status"] == "unavailable"
    assert "结算" in data["observation_reason"]  # explains why, in Chinese


def test_overview_carries_the_five_second_verdicts(client: TestClient) -> None:
    data = client.get("/api/v1/overview").json()["data"]
    assert data["collector"]["health_status"] == "healthy"
    assert data["shadow"]["health_status"] in ("healthy", "degraded")
    assert data["active_markets"] == 33
    assert data["active_markets_source"] == "shadow-report"
    assert data["subscribed_tokens"] == 66
    assert data["current_cycle_decisions"]["duration_ms"] == 812
    assert data["decision_totals"]["accepted"] == 3
    assert data["system_health"]["rest"]["state"] == "ok"
    assert data["system_health"]["ws"]["connected"] is True
    # no equity curve anywhere - the report itself forbids it
    assert data["report"]["equity_curve"] is None


def test_system_marks_unavailable_metrics_as_such(client: TestClient) -> None:
    data = client.get("/api/v1/system").json()["data"]
    joined = " ".join(data["notes"])
    assert "暂未接入" in joined  # shadow db size (metrics the producers do not send)
    assert data["storage"]["collector_db_bytes"] == 19_148_800
    assert data["storage"]["shadow_db_bytes"] is None
    metrics = client.get("/api/v1/collector/metrics").json()["data"]
    assert metrics["counters"]["rest_requests_ok"] == 1860
    assert set(metrics["counters"]) <= set(
        client.get("/api/v1/collector/health").json()["data"]["stats"]
    )


def test_collector_telemetry_reaches_every_layer(client: TestClient) -> None:
    """Collector >= v0.1.2 publishes ws_queue / latency; all layers must carry
    it, and the '暂未接入' note must disappear when the data exists."""
    health = client.get("/api/v1/collector/health").json()["data"]
    assert health["telemetry"]["ws_queue"] == {
        "depth": 2,
        "high_water": 11,
        "capacity": 512,
        "frames_dropped": 0,
    }
    overview = client.get("/api/v1/overview").json()["data"]
    assert overview["system_health"]["ws"]["queue"]["high_water"] == 11
    system = client.get("/api/v1/system").json()["data"]
    joined = " ".join(system["notes"])
    assert "WS 队列" not in joined, "the absence note must not sit next to the data"
    assert "Shadow 数据库大小" in joined  # this one really is still absent


def test_legacy_collector_without_telemetry_keeps_the_absence_note(
    tmp_path: Path, settings_for
) -> None:
    payload = collector_heartbeat(now=utcnow())
    payload.pop("telemetry", None)
    root = build_tree(tmp_path, collector=payload)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        health = client.get("/api/v1/collector/health").json()["data"]
        assert health["telemetry"] == {}
        system = client.get("/api/v1/system").json()["data"]
        assert "WS 队列" in " ".join(system["notes"])


def test_trend_sampling_picks_up_telemetry() -> None:
    from weatherquant.dashboard.trends import TrendBuffer

    sample = TrendBuffer.collect(
        now=utcnow(),
        collector_payload={
            "stats": {"rest_requests_failed": 1},
            "telemetry": {
                "ws_queue": {"depth": 5, "high_water": 9, "capacity": 256, "frames_dropped": 1},
                "rest_latency_last_sweep_ms": 12.5,
            },
        },
        shadow_payload=None,
        report_as_of=None,
        report_stale_after=None,
    )
    assert sample["ws_queue_depth"] == 5
    assert sample["ws_frames_dropped"] == 1
    assert sample["rest_latency_ms"] == 12.5
    assert sample["rest_requests_failed"] == 1


def test_disclaimer_is_on_every_decisions_payload(client: TestClient) -> None:
    data = client.get("/api/v1/decisions").json()["data"]
    assert "不代表已验证的盈利机会" in data["disclaimer"]


# ---------------------------------------------------------------------------
# validation, rate limit, safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/markets?limit=0",
        "/api/v1/markets?limit=99999",
        "/api/v1/markets?status=whatever",
        "/api/v1/markets?date=not-a-date",
        "/api/v1/decisions?state=WHATEVER",
        "/api/v1/decisions?mode=MAYBE",
        "/api/v1/decisions?limit=-1",
    ],
)
def test_invalid_parameters_are_rejected(client: TestClient, url: str) -> None:
    assert client.get(url).status_code == 422, url


def test_rate_limit_returns_429(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path)
    app = create_app(settings_for(root, rate_limit_per_minute=5))
    with TestClient(app) as client:
        codes = [client.get("/api/v1/overview").status_code for _ in range(8)]
        assert 429 in codes
        # healthz is the container probe and is deliberately not metered
        assert client.get("/healthz").status_code == 200


def test_responses_never_leak_host_paths(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, producer_snapshots=True)
    app = create_app(settings_for(root))
    # Compare with separators removed: JSON escapes backslashes, and a leak
    # that only matches after un-escaping is still a leak.
    leak = str(root).replace("\\", "")
    with TestClient(app) as client:
        for url in [*ALL_ENDPOINTS, "/api/v1/markets/m-81", "/healthz"]:
            text = client.get(url).text.replace("\\", "")
            assert leak not in text, f"{url} leaked {leak}"


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_api_calls_never_modify_the_data_tree(tmp_path: Path, settings_for) -> None:
    root = build_tree(tmp_path, producer_snapshots=True)
    before = _tree_digest(root)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        for url in [*ALL_ENDPOINTS, "/api/v1/markets/m-81", "/api/v1/markets?q=81"]:
            assert client.get(url).status_code == 200
    after = _tree_digest(root)
    assert before == after


def test_api_never_opens_a_database(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The structural guarantee: no endpoint may create a DuckDB handle."""
    import duckdb

    def refuse(*_args, **_kwargs):
        raise AssertionError("dashboard attempted to open a database")

    monkeypatch.setattr(duckdb, "connect", refuse)
    for url in [*ALL_ENDPOINTS, "/api/v1/markets/m-81", "/healthz"]:
        assert client.get(url).status_code == 200, url


def test_dashboard_package_contains_no_database_calls() -> None:
    """Static twin of the runtime test above, so CI catches it without a server.

    ``producer.py`` is the documented exception - and even it only *receives*
    an injected connection; no file in the package may import duckdb or call
    ``duckdb.connect``.
    """
    import weatherquant.dashboard as package

    package_dir = Path(package.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_dir.glob("**/*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in ("import duckdb", "duckdb.connect", "duckdb.sql", "read_only=True"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# time handling
# ---------------------------------------------------------------------------


def test_z_suffix_and_offset_timestamps_both_parse(tmp_path: Path, settings_for) -> None:
    now = utcnow()
    root = build_tree(tmp_path, archive=False, shadow=False, report=False)
    z_payload = collector_heartbeat(now=now, age_seconds=30.0)
    # the same 30s-old instant, written in Z form instead of +00:00
    z_payload["updated_at_utc"] = (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    write_json(root / "collector" / "health" / "heartbeat.json", z_payload)

    offset_payload = shadow_heartbeat(now=now, age_seconds=30.0)
    offset_payload["updated_at_utc"] = (now - timedelta(minutes=1)).isoformat()  # +00:00 form
    write_json(root / "shadow" / "health" / "heartbeat.json", offset_payload)

    app = create_app(settings_for(root))
    with TestClient(app) as client:
        collector = client.get("/api/v1/collector/health").json()
        assert collector["freshness"]["state"] == "fresh"
        assert collector["freshness"]["age_seconds"] == pytest.approx(30.0, abs=5.0)
        shadow = client.get("/api/v1/shadow/health").json()
        assert shadow["freshness"]["age_seconds"] == pytest.approx(60.0, abs=5.0)


def test_age_is_measured_against_utc_now(tmp_path: Path, settings_for) -> None:
    """A heartbeat stamped 90s ago must not look fresh just because the
    machine's local clock reads another zone."""
    root = build_tree(tmp_path, collector_age_seconds=90.0, archive=False)
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        body = client.get("/api/v1/collector/health").json()
        assert body["freshness"]["age_seconds"] == pytest.approx(90.0, abs=10.0)
        assert body["freshness"]["state"] == "fresh"  # < 120s window
        stale_body_health = client.get("/api/v1/collector/health").json()
        # serialised timestamps are UTC; either spelling counts
        assert stale_body_health["data"]["updated_at_utc"].endswith(("+00:00", "Z"))
