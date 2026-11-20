# Release pinning, snapshots and rollback

Every production stack runs an **immutable** image:

| Stack | Compose file | Variable in the stack `.env` |
|---|---|---|
| collector | `docker/compose.unraid.yaml` | `COLLECTOR_SHA` |
| shadow | `docker/compose.shadow.unraid.yaml` | `SHADOW_SHA` |
| paper | `docker/compose.paper.unraid.yaml` | `PAPER_SHA` |
| dashboard | `docker/compose.dashboard.unraid.yaml` | `DASHBOARD_SHA` |
| MCP gateway | `docker/compose.mcp.unraid.yaml` | `MCP_SHA` |

The image reference is `sha-${<STACK>_SHA}` and the container's `GIT_SHA` /
`IMAGE_TAG` come from the same value, so a running container cannot report a
build other than the one it is running. Compose refuses to start while the
variable is unset - it never falls back to `stable` or `latest`. The release
workflows publish `sha-<full commit sha>` before they move the mutable
`stable` / `shadow-stable` / `dashboard-stable` pointer, so the SHA a stack
pins always exists.

## Deploy order

Deploy in dependency order, and only when the previous stack is healthy:

1. **collector** - owns the database and the raw archive. Nothing else can be
   judged healthy until it is producing heartbeats.
2. **shadow** - reads the collector's archive. Starting it against an absent
   or empty archive produces `COLLECTOR_DISCONNECTED` and meaningless rows.
3. **paper** (when its stack lands) - consumes the signals the shadow
   publishes; same dependency on the engine below it.
4. **dashboard** - reads all of the above. It is last on purpose: it is the
   thing that explains an outage, so it must not be the thing restarted into
   one. Restarting it cannot break the engines, but a dashboard showing
   half-deployed data is worse than one showing the previous state.

The MCP gateway is independent (it reads the dashboard's HTTP API) and is
updated after the dashboard.

## Snapshot before any change

A rollback is only one step if the previous values were recorded. Snapshot
before every deploy, upgrade or configuration change:

```bash
uv run python scripts/stack_snapshot.py \
  --out /mnt/user/backups/wq-snapshots \
  --stack collector=/mnt/user/appdata/compose-manager/weatherquant-next \
  --stack shadow=/mnt/user/appdata/compose-manager/weatherquant-shadow \
  --stack paper=/mnt/user/appdata/compose-manager/weatherquant-paper \
  --stack dashboard=/mnt/user/appdata/compose-manager/weatherquant-dashboard \
  --inspect /tmp/collector-inspect.json \
  --performance /mnt/user/appdata/weatherquant-next/performance
```

`--inspect` is optional and repeatable:

```bash
docker inspect weatherquant-collector > /tmp/collector-inspect.json
docker inspect weatherquant-shadow    > /tmp/shadow-inspect.json
```

Each run writes a new directory named for the UTC instant
(`20261120T120000Z/`) containing:

```
snapshot.json          # pinned variables, resolved images, digests, generation id
collector/compose.yaml # byte-for-byte copies
collector/.env
shadow/...
```

The snapshot is **read-only on data**: it never opens a DuckDB file, never
copies a WAL file and never copies or moves an archive. A snapshot directory
that contains a database is a bug, not a backup - use the collector's own
verified backup for data (see `docs/unraid-collector-deployment.md` §9).

## Rollback

```bash
# 1. Dry run - prints the exact .env diff, writes nothing.
uv run python scripts/stack_rollback.py --snapshot /mnt/user/backups/wq-snapshots/20261120T120000Z

# 2. Apply - rewrites ONLY the .env files, atomically.
uv run python scripts/stack_rollback.py --snapshot ... --apply

# 3. Recreate the containers so compose reads the restored values.
cd /mnt/user/appdata/compose-manager/weatherquant-next && docker compose up -d
```

`--apply` never touches a compose file, a database, a WAL file or an archive.
If a stack directory no longer exists, the command fails before writing
anything, so a partial rollback cannot happen.

### Paired Dashboard + Legacy generation rollback

The dashboard image and the published performance generation are a pair. A
dashboard built before the detail was partitioned cannot read a generation
that publishes it that way, and moving only one of them answers 500 rather
than rolling back. When the snapshot recorded a generation,
`stack_rollback.py` prints it and the exact command:

```bash
uv run python scripts/pin_performance_generation.py \
  --performance /mnt/user/appdata/weatherquant-next/performance \
  --generation 20261115T120000Z-abcdef012345 --apply
```

Re-pin the dashboard image in the same window as the generation. The tool
reports each generation's format family (`bounded-detail` / `legacy-detail`)
so the pair can be checked before anything is moved.

## Rollback drill

Run this before the first supervised live run, and quarterly afterwards. Use a
maintenance window; every timestamp below is UTC.

1. **T+0 - Decide.** Name the bad release (commit sha) and the last known-good
   sha. Open the snapshot taken before the bad release.
2. **T+1 - Snapshot the current state** (it is about to be replaced, and the
   drill must leave a record of the failed build too).
3. **T+2 - Dry run** `stack_rollback.py --snapshot <dir>`. Read the diff: it
   must change only the `*_SHA` lines (and anything else you changed since the
   snapshot - if it is unrelated drift, stop and investigate).
4. **T+3 - Apply** `--apply`, then `docker compose up -d` per stack in the
   same order as a deploy (collector, shadow, paper, dashboard): the consumers
   then start against a restored engine rather than racing one.
5. **T+5 - Re-pin the performance generation** if the snapshot recorded one.
6. **T+8 - Verify** (below). Record the result, including any stage that
   failed.
7. **T+10 - Close** the drill: write the timeline, then take a fresh snapshot
   of the restored state.

### Timeline template

| Time (UTC) | Event | Evidence |
|---|---|---|
| T+0 | Bad build identified | `COLLECTOR_SHA=<bad sha>` |
| T+0 | Last known-good build named | `COLLECTOR_SHA=<good sha>` |
| T+1 | Pre-drill snapshot written | `<snapshot dir>` |
| T+2 | Dry-run diff reviewed | only `*_SHA` lines changed |
| T+3 | `.env` restored | `stack_rollback.py --apply` output |
| T+4 | Containers recreated | `docker compose up -d` output |
| T+5 | Generation re-pinned | `current.json` generation id |
| T+8 | Verification passed | `verify_running_image.py` = MATCH, healthchecks = healthy |
| T+10 | Post-drill snapshot written | `<snapshot dir>` |

## Verify after a deploy or rollback

```bash
# The container really is the commit the stack claims.
docker inspect weatherquant-collector > /tmp/collector-inspect.json
uv run python scripts/verify_running_image.py \
  --inspect /tmp/collector-inspect.json --sha "$COLLECTOR_SHA"

docker inspect weatherquant-dashboard > /tmp/dashboard-inspect.json
uv run python scripts/verify_running_image.py \
  --inspect /tmp/dashboard-inspect.json --sha "$DASHBOARD_SHA"
```

`verify_running_image.py` exits non-zero when the image tag is not
`sha-<git sha>`, when `GIT_SHA` / `IMAGE_TAG` disagree, or when the container
is not running. It is a pure function over the inspect JSON, so it needs no
Docker on the machine doing the review.

Then confirm the observable state:

```bash
docker ps --format '{{.Names}} {{.Status}}'          # all stacks healthy
curl -fsS http://127.0.0.1:8787/healthz              # dashboard sees its sources
docker exec weatherquant-collector cat /data/health/heartbeat.json
```

The heartbeat's `git_sha` / `image_tag` must equal the pinned variable. If
they do not, the container is running a cached image - `docker compose pull`
then `docker compose up -d --force-recreate`.

## What rollback does not do

- It does not copy, move or delete a database, WAL file or archive. The image
  contains no data; `/data` is untouched by an image change.
- It does not reverse migrations. Migrations are forward-only: a rollback is
  safe only while the schema version on disk is supported by the older code.
  Check before crossing a migration boundary
  (`docs/unraid-collector-deployment.md` §10).
- It does not restore unrelated configuration. The `.env` is restored exactly
  as it was at snapshot time; anything you changed since then is reverted too,
  which is why the dry-run diff must be read first.
