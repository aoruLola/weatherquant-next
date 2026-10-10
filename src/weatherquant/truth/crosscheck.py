"""Cross-check observations against what the venue actually resolved.

The comparison that matters
---------------------------
Two temptations to avoid, both of which produced misleading numbers during
development:

**Do not compare an observation to ``settlements.daily_max_temp_f``.** In the
legacy database that column is a copy of our own ``all_readings`` aggregate,
so the comparison is circular - it agrees 404 times out of 404 by construction.
The independent quantity is *which bracket the venue paid out on*, derived from
the venue's own ``yes_price`` per market.

**Do not group by target date.** One date can carry more than one event: the
legacy data holds an ``arch-`` prefixed duplicate for 2026-05-19 whose resolved
bracket differs from its sibling's. Grouping by date alone invents "two winners"
that are really two events, and hides the real disagreement between them.
Everything here keys on ``event_id``.

A mismatch is recorded, never reconciled. Editing an observation to fit a
settlement destroys the only evidence that something disagreed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

__all__ = ["CrosscheckResult", "Verdict", "crosscheck_events", "verdict_counts"]

# Verdicts are a closed set so a report can assert on them rather than on
# free-text that drifts.
MATCH = "match"
MISMATCH = "mismatch"
NO_OBSERVATION = "no_observation"
NO_WINNER = "no_winner"
AMBIGUOUS_WINNER = "ambiguous_winner"
UNPARSED_RULES = "unparsed_rules"
NO_RULES = "no_rules"

VERDICTS = (
    MATCH,
    MISMATCH,
    NO_OBSERVATION,
    NO_WINNER,
    AMBIGUOUS_WINNER,
    UNPARSED_RULES,
    NO_RULES,
)
Verdict = str


@dataclass(frozen=True, slots=True)
class CrosscheckResult:
    event_id: str
    target_date: date | None
    station: str
    observation_source: str
    aggregation_rule: str
    observed_max_f: float | None
    winning_bracket_label: str | None
    winning_bracket_low: float | None
    winning_bracket_high: float | None
    n_winners: int
    verdict: Verdict
    detail: str | None
    checked_at_utc: datetime

    def to_row(self) -> tuple:
        return (
            self.event_id,
            self.target_date,
            self.station,
            self.observation_source,
            self.aggregation_rule,
            self.observed_max_f,
            self.winning_bracket_label,
            self.winning_bracket_low,
            self.winning_bracket_high,
            self.n_winners,
            self.verdict,
            self.detail,
            self.checked_at_utc,
        )


CROSSCHECK_COLUMNS = (
    "event_id, target_date, station, observation_source, aggregation_rule, "
    "observed_max_f, winning_bracket_label, winning_bracket_low, winning_bracket_high, "
    "n_winners, verdict, detail, checked_at_utc"
)


def bracket_contains(low: float | None, high: float | None, value: float) -> bool:
    """Does an observed whole-degree value fall inside a venue bracket?

    The venue writes open-ended brackets as ``X°F or below`` / ``X°F or
    higher``, which are stored with one bound NULL. Getting the direction wrong
    here inverts the verdict for every open bracket - which was exactly a bug
    caught during development, so both branches are asserted in the tests.
    """
    if low is None and high is None:
        return False
    if low is None:      # "X or below": bounded above only
        return value <= high
    if high is None:     # "X or higher": bounded below only
        return value >= low
    return low <= value <= high


def _is_winner(outcome: dict) -> bool:
    """A market is a winner either because we derived the flag, or because the
    venue paid out on it.

    Accepting both matters because ``settlement_outcome`` carries ``yes_price``
    as the raw venue value and ``is_winner`` as our derivation of it; requiring
    only the derived field would silently skip rows loaded straight from the
    venue.
    """
    if "is_winner" in outcome:
        return bool(outcome["is_winner"])
    return outcome.get("yes_price") == 1.0


def _winners(outcomes: Sequence[dict]) -> list[dict]:
    return [o for o in outcomes if _is_winner(o)]


def crosscheck_events(
    *,
    events: Sequence[dict],
    observations: dict[date, float],
    station: str,
    observation_source: str,
    aggregation_rule: str,
    rules_by_event: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[CrosscheckResult]:
    """Evaluate every event. Pure - reads nothing, writes nothing.

    ``events`` yields one dict per *event* carrying its target_date and its
    markets' outcomes; ``observations`` maps a target date to the observed
    daily maximum under ``aggregation_rule``.
    """
    now = now or datetime.now(tz=UTC)
    rules_by_event = rules_by_event or {}
    by_event: dict[str, list[dict]] = defaultdict(list)
    for item in events:
        by_event[item["event_id"]].append(item)

    results: list[CrosscheckResult] = []
    for event_id, items in sorted(by_event.items()):
        target_date = items[0].get("target_date")
        rules = rules_by_event.get(event_id)
        if rules is None:
            verdict, detail, observed = NO_RULES, "no parsed rules for this event", None
            n_winners, label, low, high = 0, None, None, None
        elif getattr(rules, "parse_status", "parsed") == "unparsed":
            verdict, detail, observed = UNPARSED_RULES, "rules text could not be parsed", None
            n_winners, label, low, high = 0, None, None, None
        else:
            winners = _winners(items)
            n_winners = len(winners)
            label = winners[0].get("bracket_label") if n_winners == 1 else None
            low = winners[0].get("bracket_low") if n_winners == 1 else None
            high = winners[0].get("bracket_high") if n_winners == 1 else None
            observed = observations.get(target_date) if target_date else None

            if n_winners == 0:
                verdict, detail = NO_WINNER, "no market in this event resolved yes"
            elif n_winners > 1:
                # Almost always two events collapsed into one key by mistake, or
                # a genuinely ambiguous resolution - either way, not a match.
                labels = [w.get("bracket_label") for w in winners]
                verdict = AMBIGUOUS_WINNER
                detail = f"{n_winners} markets resolved yes: {labels}"
            elif observed is None:
                verdict, detail = NO_OBSERVATION, f"no observation for {target_date}"
            elif bracket_contains(low, high, observed):
                verdict, detail = MATCH, None
            else:
                verdict = MISMATCH
                detail = (
                    f"observed {observed:g}F is outside the winning bracket "
                    f"{label!r} [{low}, {high}]"
                )

        results.append(
            CrosscheckResult(
                event_id=event_id,
                target_date=target_date,
                station=station,
                observation_source=observation_source,
                aggregation_rule=aggregation_rule,
                observed_max_f=observed,
                winning_bracket_label=label,
                winning_bracket_low=low,
                winning_bracket_high=high,
                n_winners=n_winners,
                verdict=verdict,
                detail=detail,
                checked_at_utc=now,
            )
        )
    return results


def verdict_counts(results: Iterable[CrosscheckResult]) -> dict[str, int]:
    counts = dict.fromkeys(VERDICTS, 0)
    for result in results:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    return counts


def run_crosscheck(research_db, *, station: str = "KLGA") -> dict:
    """Load outcomes and observations from the research DB, cross-check, store.

    Runs both aggregation rules separately: the audit showed ``all_readings``
    agrees with the venue while routine-hourly-only does not, and reporting one
    blended number would hide exactly the difference that matters.

    Idempotent - ``settlement_crosscheck`` has a natural key, so re-running
    updates rather than duplicates.
    """
    con = research_db.con

    outcomes = [
        dict(zip(
            ("event_id", "target_date", "bracket_label", "bracket_low",
             "bracket_high", "is_winner"),
            row,
            strict=True,
        ))
        for row in con.execute(
            "SELECT event_id, target_date, bracket_label, bracket_low, "
            "bracket_high, is_winner FROM settlement_outcome ORDER BY event_id, market_id"
        ).fetchall()
    ]
    rules_by_event = {
        r[0]: _RuleShim(r[1])
        for r in con.execute("SELECT event_id, parse_status FROM settlement_rules").fetchall()
    }
    rules_meta = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT event_id, station FROM settlement_rules WHERE station IS NOT NULL"
        ).fetchall()
    }

    summary: dict[str, dict] = {}
    all_results: list[CrosscheckResult] = []
    for aggregation_rule, source in (
        ("max of all readings", "legacy_all_readings"),
        ("max of routine hourly METAR", "legacy_hourly_metar"),
    ):
        observations = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT target_date, max_temp_f FROM klga_daily_max "
                "WHERE aggregation_rule = ? AND station = ? AND max_temp_f IS NOT NULL",
                [aggregation_rule, station],
            ).fetchall()
        }
        results = crosscheck_events(
            events=outcomes,
            observations=observations,
            station=station,
            observation_source=source,
            aggregation_rule=aggregation_rule,
            rules_by_event=rules_by_event,
        )
        # Use the station recorded with each event's rules where available.
        patched = [
            CrosscheckResult(
                **{
                    **{
                        f: getattr(r, f)
                        for f in r.__dataclass_fields__
                        if f not in ("station",)
                    },
                    "station": rules_meta.get(r.event_id, station),
                }
            )
            for r in results
        ]
        all_results.extend(patched)
        summary[aggregation_rule] = {
            "total": len(patched),
            **{k: v for k, v in verdict_counts(patched).items() if v},
        }

    if all_results:
        con.executemany(
            "INSERT INTO settlement_crosscheck "
            f"({', '.join(CROSSCHECK_COLUMNS.split(', '))}) "
            "VALUES ("
            + ",".join("?" for _ in range(len(CROSSCHECK_COLUMNS.split(", "))))
            + ") ON CONFLICT DO UPDATE SET "
            + ", ".join(
                f"{c} = excluded.{c}"
                for c in CROSSCHECK_COLUMNS.split(", ")
                if c not in ("event_id", "station", "observation_source", "aggregation_rule")
            ),
            [r.to_row() for r in all_results],
        )

    return summary


class _RuleShim:
    """Minimal stand-in for a parsed-rules row, read from the research DB."""

    def __init__(self, parse_status: str) -> None:
        self.parse_status = parse_status
