"""Historical forecast runs from the Open-Meteo Single Runs API.

What this endpoint is, and is not
---------------------------------
It serves one *specific past run* of one model, so the response is a forecast
as it stood at initialisation - not a reanalysis and not a blend. That is the
only thing usable for a point-in-time history; ERA5, the archive API and any
"best available" product are hindsight and are deliberately not used here.

Three verified constraints shape everything below (see
``docs/weather-data-sources.md`` for the measurements behind each):

* The host is ``single-runs-api`` (plural). ``single-run-api`` does not resolve;
  ``api.open-meteo.com`` and ``archive-api.open-meteo.com`` reject ``run``.
* A bare ``models=gfs`` is rejected. The working GFS ids are ``gfs_seamless``
  and ``gfs_global``.
* The response contains **no publication time**. Publication must be estimated,
  and an estimate is recorded as such rather than as fact.

Nothing here interpolates a missing run. If a run is unavailable, it is recorded
as unavailable with the venue's own error text; a gap in the record is a result,
not a defect to fill in.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from weatherquant.data.http import HttpStatusError, ResilientHttp

__all__ = [
    "BASE_URL",
    "DEFAULT_LATITUDE",
    "DEFAULT_LONGITUDE",
    "MODEL_IDS",
    "PUBLISHABLE_LAG_HOURS",
    "RunFetch",
    "SingleRunsClient",
    "canonical_payload_hash",
    "parse_run_response",
]

BASE_URL = "https://single-runs-api.open-meteo.com/v1/forecast"

# LaGuardia Airport. Responses echo the grid cell they actually used, which is
# asserted - a model change that moves the cell silently changes what we are
# measuring, and a 10 km shift is larger than many of the effects being studied.
DEFAULT_LATITUDE = 40.7794
DEFAULT_LONGITUDE = -73.8803

MODEL_IDS = {
    "ecmwf_ifs025": "ecmwf_ifs025",
    "gfs": "gfs_seamless",  # a bare `gfs` is rejected with HTTP 400
}

# Publication is not in the response, so it is *estimated* as run_time + this
# lag, and every row derived from it is marked publishable_basis='estimated'.
# The lag is chosen to exceed the slowest availability observed while
# verifying this endpoint (ecmwf_ifs025 12Z still unavailable at 6.6h on
# 2026-10-10) with margin. It is a safety delay, not a measurement.
PUBLISHABLE_LAG_HOURS = 12.0

_RUN_FORMAT = "%Y-%m-%dT%H:%M"
_GRID_DRIFT_TOLERANCE_DEG = 0.05


@dataclass(frozen=True, slots=True)
class HourlyPoint:
    valid_time_utc: datetime
    temperature_2m: float | None


@dataclass
class RunFetch:
    """The result of asking for one historical run."""

    model: str
    run_time_utc: datetime | None
    status: str                      # 'ok' | 'unavailable' | 'failed'
    http_status: int | None = None
    error_text: str | None = None
    hourly: list[HourlyPoint] = field(default_factory=list)
    grid_latitude: float | None = None
    grid_longitude: float | None = None
    utc_offset_seconds: int | None = None
    payload_sha256: str | None = None
    raw_path: str | None = None
    n_null: int = 0
    first_valid_utc: datetime | None = None
    last_valid_utc: datetime | None = None
    fetched_at_utc: datetime | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def n_points(self) -> int:
        return len(self.hourly)


def canonical_payload_hash(payload: Any) -> str:
    """SHA-256 of a response with the volatile field removed.

    ``generationtime_ms`` is the server's own processing time and changes on
    every identical request, so hashing the body as-is would make an unchanged
    upstream look like changing data. Everything else is hashed verbatim.
    """
    if isinstance(payload, dict):
        stable = {k: v for k, v in payload.items() if k != "generationtime_ms"}
        blob = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    else:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_run_response(
    payload: Any,
    *,
    model: str,
    expected_latitude: float = DEFAULT_LATITUDE,
    expected_longitude: float = DEFAULT_LONGITUDE,
) -> RunFetch:
    """Turn one API response into a :class:`RunFetch`.

    The run time is *derived*, because the response does not carry one: the
    series begins at the initialisation instant, so ``hourly.time[0]`` is it.
    When ``timezone`` was requested, ``hourly.time`` is local and the offset is
    applied to recover UTC.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"response is {type(payload).__name__}, expected an object")

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    offset = int(payload.get("utc_offset_seconds") or 0)

    grid_lat = payload.get("latitude")
    grid_lon = payload.get("longitude")

    result = RunFetch(
        model=model,
        run_time_utc=None,
        status="ok",
        grid_latitude=float(grid_lat) if grid_lat is not None else None,
        grid_longitude=float(grid_lon) if grid_lon is not None else None,
        utc_offset_seconds=offset,
        payload_sha256=canonical_payload_hash(payload),
    )

    if not times:
        result.status = "failed"
        result.error_text = "response contained no hourly time axis"
        return result

    points: list[HourlyPoint] = []
    for raw_time, raw_temp in zip(times, temps, strict=False):
        try:
            local_or_utc = datetime.fromisoformat(str(raw_time))
        except ValueError:
            continue
        # `hourly.time` has no zone; the offset tells us which clock it is on.
        valid_utc = (local_or_utc - timedelta(seconds=offset)).replace(tzinfo=UTC)
        value: float | None
        if raw_temp is None:
            value = None
        else:
            try:
                value = float(raw_temp)
            except (TypeError, ValueError):
                value = None
        points.append(HourlyPoint(valid_time_utc=valid_utc, temperature_2m=value))

    if not points:
        result.status = "failed"
        result.error_text = "response contained a time axis but no parsable points"
        return result

    result.hourly = points
    # The series starts at initialisation, so the first stamp IS the run time -
    # including when its temperature is null (see the 06Z/18Z tail effect).
    result.run_time_utc = points[0].valid_time_utc
    valid = [p for p in points if p.temperature_2m is not None]
    result.n_null = len(points) - len(valid)
    if valid:
        result.first_valid_utc = valid[0].valid_time_utc
        result.last_valid_utc = valid[-1].valid_time_utc
    return result


class SingleRunsClient:
    """Read-only client for one historical run at a time."""

    def __init__(
        self,
        http: ResilientHttp,
        *,
        latitude: float = DEFAULT_LATITUDE,
        longitude: float = DEFAULT_LONGITUDE,
        timezone_name: str = "America/New_York",
        forecast_hours: int = 240,
        raw_dir: Path | None = None,
    ) -> None:
        self._http = http
        self.latitude = latitude
        self.longitude = longitude
        self.timezone_name = timezone_name
        self.forecast_hours = forecast_hours
        self.raw_dir = raw_dir

    def build_url(self, model: str, run_time_utc: datetime) -> tuple[str, dict[str, Any]]:
        run = model if model in MODEL_IDS.values() else MODEL_IDS.get(model, model)
        params = {
            "latitude": f"{self.latitude:.4f}",
            "longitude": f"{self.longitude:.4f}",
            "hourly": "temperature_2m",
            "models": run,
            "run": run_time_utc.astimezone(UTC).strftime(_RUN_FORMAT),
            # httpx percent-encodes this; pre-encoding it produced
            # America%252FNew_York and a 400 "Invalid timezone".
            "timezone": self.timezone_name,
            "forecast_hours": str(self.forecast_hours),
        }
        return BASE_URL, params

    def fetch(self, model: str, run_time_utc: datetime) -> RunFetch:
        """Fetch one run. Never raises for an unavailable run - that is a result."""
        url, params = self.build_url(model, run_time_utc)
        fetched_at = datetime.now(tz=UTC)
        try:
            payload = self._http.get_json(url, params=params, limiter="open_meteo")
        except HttpStatusError as exc:
            # Open-Meteo reports an unavailable run as HTTP 400 with a message
            # in the body, and uses the *same* body for three different causes
            # (a future run, one past the archive cut-off, and a time that is
            # not an issue time). They are recorded verbatim and never
            # distinguished, because pretending to know which it was would be
            # an invention.
            detail = exc.detail
            unavailable = "not available" in detail or "must not be set" in detail
            return RunFetch(
                model=model,
                run_time_utc=run_time_utc,
                status="unavailable" if unavailable else "failed",
                http_status=400,
                error_text=detail[:500],
                fetched_at_utc=fetched_at,
            )
        except Exception as exc:
            return RunFetch(
                model=model,
                run_time_utc=run_time_utc,
                status="failed",
                error_text=str(exc)[:500],
                fetched_at_utc=fetched_at,
            )

        if payload is None:
            # 404 from this endpoint is not used; kept for symmetry with other
            # clients. Open-Meteo reports unavailability as 400.
            return RunFetch(
                model=model, run_time_utc=run_time_utc, status="unavailable",
                http_status=404, error_text="not found", fetched_at_utc=fetched_at,
            )

        if not isinstance(payload, dict) or "hourly" not in payload:
            # Open-Meteo answers an unavailable run with 400 + a message in the
            # body; the transport raises HttpStatusError, so reaching here with
            # no `hourly` means a successful-but-empty response.
            error = None
            if isinstance(payload, dict):
                error = str(payload.get("reason") or payload.get("error") or "")[:500] or None
            return RunFetch(
                model=model, run_time_utc=run_time_utc, status="unavailable",
                error_text=error, fetched_at_utc=fetched_at,
            )

        result = parse_run_response(payload, model=model)
        result.fetched_at_utc = fetched_at
        result.raw_path = self._store_raw(model, run_time_utc, payload)
        return result

    def _store_raw(self, model: str, run_time_utc: datetime, payload: Any) -> str | None:
        if self.raw_dir is None:
            return None
        day = run_time_utc.astimezone(UTC).date()
        directory = self.raw_dir / model / f"{day.isoformat()}"
        directory.mkdir(parents=True, exist_ok=True)
        name = run_time_utc.astimezone(UTC).strftime("%Y%m%dT%H%MZ") + ".json"
        path = directory / name
        # Write once: a re-fetch of the same run must not rewrite history.
        if not path.exists():
            path.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
        return str(path.relative_to(self.raw_dir))


def run_time_from_label(label: str) -> datetime | None:
    """Parse the ``run`` query value (``yyyy-mm-ddThh:mm``, no zone)."""
    try:
        return datetime.strptime(label, _RUN_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def model_for(label: str) -> str:
    """Resolve a configured model alias to the id the API accepts."""
    return MODEL_IDS.get(label, label)
