# Handover — Performance streaming refactor

Written 2026-10-15 by the previous session. Everything below is either a verified
fact or explicitly marked as reasoning that has not been verified.

---

## 1. Where the work is

**Branch `feat/streaming-performance`**, based on `origin/main` = `635acb5`.

Two commits, both finished and tested:

| commit | what |
|---|---|
| `77fcdb7` | `fix(performance)`: rename the two distinct-market counts |
| `11833d3` | `docs(performance)`: record the two production incidents in `docs/performance-deployment.md` §10 |

The streaming engine itself is **not started**. Nothing is half-written.

### What is already done and verified

- **Field rename (item 6 of the brief) — complete.** `coverage.all_decision_distinct_markets`
  / `coverage.all_decision_distinct_target_dates` (population = every decision read)
  versus `summary.accept_opportunity_distinct_markets` /
  `accept_opportunity_distinct_target_dates` (population = opportunities).
  The old `summary` names survive as deprecated aliases because a **deployed**
  dashboard image (`sha-e6698df…`) still reads them; `coverage` gets no alias
  because it was added in #10 and has no consumers yet.
  `win_rate.overall.distinct_markets` is a **third** count of the same shape
  (population = RESOLVED+VERIFIED episodes). Left alone deliberately; it is
  documented as such in `docs/performance-data-contract.md` §2.5.
  Frontend updated (`dashboard/frontend/src/pages/Performance.tsx`), fixtures
  updated, typecheck clean, 81 frontend tests pass, 958 Python tests pass.

- **Temp cleanup (item 7) — complete.** `/mnt/user/appdata/wq-patch/` held exactly
  two files, both verified superseded (`episodes.py` byte-identical to main;
  `export.py` an intermediate revision lacking the persist fix and all of PR #10),
  neither mounted by any container. Both deleted, empty directory removed. Every
  other `weatherquant-shadow-*` directory on the NAS was **left alone** — some
  predate this work and their purpose is unconfirmed.

- **P1 export stall — fixed and deployed.** See §4.

---

## 2. The task that remains

The full brief is in the user's message; the essentials:

Replace the all-in-memory `build_performance()` with a **streaming incremental
engine with persisted research state and bounded memory**. Specifically:

- read committed export parts in bounded batches; persist a consumption watermark;
  resume after a crash; idempotent on re-input; **refuse to advance if a part is
  missing**; support first backfill, incremental updates, and bounded recompute
- carry episode state across batches (an episode split by a batch boundary must
  not become two episodes; duplicate input must not inflate counts or edges;
  LIVE/REPLAY isolated)
- a research state store separate from the production shadow DB (DuckDB / SQLite /
  Parquet — **justify the choice**)
- settlement status can change later, so simple accumulation over new rows is
  wrong — support partial recompute
- reference capital must keep full time ordering, per-market caps, allocation and
  release events
- **must not use business time alone to decide what has been consumed**

Acceptance: ≤2 GiB for the research container (or evidence for a different limit);
stress at 1.5M / 3M / 5M synthetic rows; peak RSS must not grow linearly with
accumulated history; **no `list()` of all history, no full Python-dict load**.

Correctness: on a small fixed dataset, compare the streaming engine against the
old implementation on **ten** items — ACCEPT observation count, episode count and
identity, Observation Edge Sum, Unique Opportunity Edge Sum, win-rate numerator
and denominator, clustered confidence interval, 100 USDC reference capital,
no-duplicate/no-gap after interruption and resume, multi-batch == single-batch,
and N/A still shown when data is missing. **If the algorithm has to change,
state the change in scope explicitly — do not hide it as "floating point
difference".**

---

## 3. Hard constraints (from the brief, do not violate)

- Collector and Shadow keep running.
- Do not modify, clear or rebuild the production DuckDB.
- Do not modify raw JSONL, historical decision exports, or existing backups.
- **Do not run a full build on the production NAS again** — it has already been
  shown to make the host unresponsive. All stress testing in isolation.
- Do not improve performance by relaxing tests, deleting data, or dropping history.
- MCP: offline work only. **No image release, no deployment, no new public entry.**
- Production deploy, production backfill and service restarts each need fresh
  approval.

---

## 4. Production state at handover (verified)

All three containers healthy.

| component | image | notes |
|---|---|---|
| shadow | `ghcr.io/aorulola/weatherquant-next-shadow:sha-635acb531c137a901e2d6f0df2cbab4b72a115a9` | LIVE, `fatal_error: null` |
| dashboard | `…-dashboard:sha-e6698dfd39930bf1dd219410ae911b1cf496a982` | |
| collector | `ghcr.io/aorulola/weatherquant-next:stable` | never stopped |

**Decisions export — caught up.** `rows_exported` 1,481,414, watermark
`2026-10-15T05:45:04Z` (i.e. current), 79 parts, recent ticks ~1,650 rows
(the live rate). Integrity verified at full scale: 1,481,414 rows, 1,481,414
unique ids, **0 duplicates, 0 cursor-order violations**.

**Performance generation** `20261015T044255Z-5d2adcd1654c`, built from
1,020,000 decisions, with a real `watermark` and `coverage` (see §6).

**Backups** in `/mnt/user/appdata/weatherquant-shadow/backups/`:
`shadow-20261015T034343Z.duckdb` (current, MD5-verified, opens read-only,
1,453,232 decisions) plus three older ones. **Do not delete these.**

### The two incidents, with numbers

**Export stall (fixed, PR #9).** `DecisionExporter._load()` recounted rows by
reading every part, inside the tick's wall-clock budget: `_count_rows` measured
**5.287s** against `shadow_export_max_seconds = 5.0`. It crossed over at part 35,
which is exactly where the stall began. **It is intermittent, not permanent** —
it advanced one part on its own at 03:04 while being diagnosed, so "the watermark
has not moved" is not a reliable detector; the margin against the budget is.
Fixed by counting only the parts the state does not already cover (steady state
now **0.01s**), persisting the recovered state, consulting the budget only after
a part is written, and recording a `decision_export_no_progress` quality event.
After the upgrade the first tick wrote 60,000 rows in 7 seconds.

**Rebuild memory wall (NOT fixed — this is the task).** ~6.8 KB per row held as a
Python dict:

| rows | need | result |
|---|---|---|
| 700,000 | ~4.6 GB | 90s, fine |
| 960,000 | ~6.8 GB | OOM at a 6.5 GB cap |
| 960,000 | — | succeeded at 7.5 GB but took **35 minutes** and the **host was unreachable for all of it** |
| 1,461,680 | ~9.5 GB + ~4 GB for shadow/collector | exceeds the 16 GB host |

The lesson worth keeping: **a memory cap protects the container, not the host.**
The 7.5 GB run did not OOM the container and the host was still dead for 35 min.

---

## 5. Things that will bite you (verified gotchas)

1. **Single-writer DuckDB lease.** The shadow holds an exclusive lease on
   `shadow.duckdb` for its whole life; a second connection is refused *even
   read-only*. To inspect the DB, open a **backup copy** read-only — that is what
   I did, and it works. Never open the live file.

2. **Two field spellings for one decision.** The decisions export carries the
   engine's column names (`decision_time_utc`, `net_edge`); the performance
   reader wants the public ones (`decided_at_utc`, `net_edge_usd`).
   `episodes._field()` resolves both — use it, do not hand-roll a third spelling.
   This seam was a P1 bug once already.

3. **The export cursor is forward-only.** REPLAY writes rows with *historical*
   `decision_time_utc`. A row written after the cursor has passed its instant is
   **never** exported. Any "no gaps" claim is only valid for the range *before*
   the cursor. This is documented as an open risk, not solved.

4. **`activity_between` is inherently global.** `build_episodes` decides
   `GAP_DATA_MISSING` vs `GAP_SIGNAL_BREAK` by asking whether *anything* was
   decided between two instants, across all markets, via a binary search over
   every decision instant. A batch-at-a-time fold cannot answer that locally.
   It is only reached when `gap > gap_window_seconds` (900s), so it is rare — but
   it must be answered exactly. Options: a query against the persisted store, or
   a bounded structure tracking, per open episode, the first instant seen after
   its last observation.

5. **Exact quantiles.** The edge payload publishes `p05/p25/p50/p75/p95`, median
   and `std` — not just sums. These need the full value distribution. Do **not**
   substitute an approximation without declaring the scope change (item 5 forbids
   hiding it). A DuckDB store can compute exact quantiles with spilling.

6. **`book_for` is `lambda _e: None` in production**, so every opportunity is
   `NOT_EXECUTABLE` with reason `NO_BOOK`. That is correct behaviour — the export
   carries no retained book levels. Do not "fix" it by assuming fills.

7. **Reading the export races the publisher.** Always take rows *and* the cursor
   from **one** enumeration — `read_export_with_watermark()`. Two scans produced a
   watermark past the rows actually read, which is a false coverage claim
   (Codex caught this; it is fixed).

8. **The export refuses a gap.** `read_export_with_watermark()` requires part
   indices contiguous from 1 and raises otherwise. Keep that: a cursor is a
   high-water mark, so reading past a missing part claims rows that are not there.

9. **Windows clock granularity.** `time.monotonic()` on Windows can return the
   same value twice, so a test using a tiny `max_seconds` reads as zero elapsed
   and passes against the bug it is meant to catch. It did, once. Drive
   `time.monotonic` explicitly in budget tests.

---

## 6. Reference: the current pipeline

`build_performance_snapshots()` (in `performance/backfill.py`) →
`build_performance()` (`performance/snapshot.py:133`) → six payloads.

- `build_episodes(rows)` (`performance/episodes.py:395`) — the state machine.
  Sorts globally by `decided_at_utc`; folds into `open_by_key`; closes on
  `GAP_DATA_MISSING` / `GAP_SIGNAL_BREAK` (gap > window), `STATE_CHANGE`
  (a non-ACCEPT state), or `END_OF_DATA`. Identity key is
  `(market_id, target_date, model_name, direction, mode)`.
  `_OpenEpisode.observe()` accumulates count, edges, and first-seen fields.
- `observation_edge()` (`edge.py`) — ACCEPT rows with a usable `net_edge_usd`;
  rows with a null edge are *refused*, never summed as zero.
- `win_rate()` / `assign_outcomes()` (`winrate.py`) — denominator is
  RESOLVED+VERIFIED episodes only; cluster bootstrap by `target_date` with
  `random.Random(20261013)`, 2000 iterations.
- `simulate_portfolio()` (`capital.py`) — episodes in `first_seen_utc` order,
  per-market cap, `available = min(cap - market_used, balance)`.
- `publish_performance()` writes `generations/<id>/` + `manifest.json` +
  atomic `current.json`.

`manifest.json` now carries a real `watermark` (cursor of the input) and
`coverage` (decisions, all-decision distinct markets/target dates, first/last
decision instant), both read from the same enumeration as the rows.

---

## 7. Suggested approach (reasoning, NOT verified)

A design that fits the constraints — treat as a starting point, not a plan:

- **DuckDB as the research store**, on its own file, separate from the shadow DB.
  Justification to write up: it is already a dependency, it gives ACID appends,
  exact aggregates *including exact quantiles* with spilling to disk, and it
  answers the global questions (`activity_between`, distinct counts) by query
  rather than by holding history in memory. SQLite was the alternative; it has
  no native `quantile` and weaker analytical spill behaviour.
- **Keep the episode fold in Python**, streaming, carrying only the *open*
  episodes across batches (in the real data `open_at_end` was 16 — the open set
  is small and does not grow with history). Persist the open set in the
  checkpoint between runs.
- **Store observations and episodes in the research store**, then compute the six
  payloads with SQL over it. Memory then scales with the open-episode count and
  the batch size, not with history.
- **Prove equivalence before optimising further.** Build the small-dataset harness
  first and run the old and new engines side by side on it. Ten checks in the
  brief; they are cheap to write and they are the only thing that makes the
  refactor safe.

Two things I would verify before committing to that design:
`activity_between`'s exact semantics under batching (gotcha 4), and whether
quantiles over the store match `statistics.pstdev` / the existing percentile
function bit-for-bit — if not, say so explicitly rather than papering over it.

---

## 8. Open items carried over (not this task)

- **Version drift**: `pyproject.toml` `0.1.3`, `src/weatherquant/__init__.py`
  `0.1.0`, `uv.lock` `0.1.3`. Images self-report inconsistently. Fixing it means
  touching the lockfile.
- **`manifest.coverage` vs `summary` counts**: now renamed, but `coverage`
  and the win-rate still describe three different populations — read §2.5 of
  `docs/performance-data-contract.md` before touching either.
- **MCP**: `mcp-release.yml` exists on main (PR #8) and has never been run. 13
  read-only tools, 44 tests. No image, no container. Release and deploy both need
  approval.
- **The export's forward-only cursor** (gotcha 3) is an unsolved correctness risk.

---

## 9. Loose end from this session

A background analysis agent (id `a933971e7cc747078`) was asked for a precise
specification of what the streaming engine must accumulate — per-aggregate
streamability, the win-rate cluster bootstrap state, capital's time ordering, and
a list of every global-view dependency. It had not reported when this handover
was written. Its findings were never seen or verified, so treat them as
unavailable; re-derive rather than assume.
