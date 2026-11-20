# Paper engine on Unraid

This guide pairs with `docker/compose.paper.unraid.yaml`. Paper uses its own
container, image, state directory and DuckDB. Stopping Paper never stops the
collector or Shadow, and Paper cannot write either one's database or raw/published
inputs.

## 1. Prerequisites and paths

- Collector raw JSONL is under `/mnt/user/appdata/weatherquant-next/raw`.
- Shadow publishes decision parts under
  `/mnt/user/appdata/weatherquant-shadow/reports/decisions-export/parts`.
- Compose Manager Plus is installed.
- The selected `paper-vX.Y.Z` release has passed `paper-release.yml`.

Create only Paper's writable directory. Do not change ownership or permissions on
the collector or Shadow directories; both are mounted read-only.

```bash
mkdir -p /mnt/user/appdata/weatherquant-paper/{paper,health,backups}
chown -R 1000:1000 /mnt/user/appdata/weatherquant-paper
```

The container runs as uid/gid `1000:1000`. If the NAS uses another owner, change
both this command and the Compose `user:` value together.

## 2. Configure the stack

Copy `docker/compose.paper.unraid.yaml` into its own Compose Manager Plus stack.
Set these values in the stack `.env`:

```dotenv
# Full commit SHA published as sha-<full sha>; not paper-stable or latest.
PAPER_SHA=0123456789abcdef0123456789abcdef01234567
# The NAS's own LAN address. Do not use 0.0.0.0.
PAPER_API_LAN_IP=192.168.1.20
# Keep false while preparing; enable deliberately below.
WQ_PAPER_ENABLED=false
```

The image, `GIT_SHA`, and `IMAGE_TAG` all derive from `PAPER_SHA`. The API is GET-only
and is published as `PAPER_API_LAN_IP:8790`, not on every host interface. There are
no order, wallet, or signing endpoints.

Pull the explicitly pinned image:

```bash
docker compose --env-file .env -f compose.paper.unraid.yaml pull
```

## 3. First start: initialize, then enable

The account must exist before `serve` starts. Initialization is a write operation,
so it also requires explicit enablement. From the stack directory, run this once:

```bash
docker compose --env-file .env -f compose.paper.unraid.yaml \
  run --rm --no-deps -e WQ_PAPER_ENABLED=true paper init
```

Expected output includes `account paper-main: opened with 100.0 USDC` (or
`already open` on a later invocation). Repeated initialization does not add funds.

Now edit the stack `.env`:

```dotenv
WQ_PAPER_ENABLED=true
```

Start Paper:

```bash
docker compose --env-file .env -f compose.paper.unraid.yaml up -d
```

Leaving the flag false is intentional fail-closed behavior: `serve` refuses to run
and explains that `WQ_PAPER_ENABLED` is not true. Do not treat that refusal as an
image failure.

## 4. Routine start and stop

```bash
# Start or update only Paper.
docker compose --env-file .env -f compose.paper.unraid.yaml up -d paper

# Stop only Paper; collector and Shadow continue untouched.
docker compose --env-file .env -f compose.paper.unraid.yaml stop paper

# Start the same stopped container again.
docker compose --env-file .env -f compose.paper.unraid.yaml start paper
```

`stop` sends SIGTERM; `serve` handles it like Ctrl-C - the current tick's
transaction rolls back, the run is closed and the lease released - and the
container exits 0 within seconds. An exit code of 137 means the 60-second
grace period ran out and Docker had to kill it; check the logs.

Do not run these commands against `compose.unraid.yaml` or
`compose.shadow.unraid.yaml`. A second Paper process against the same state is
refused with exit code 4; that is single-instance protection, not a crash.

## 5. First-hour checks

Run these checks immediately and again after the first hour:

1. **Process, user and restart state**

   ```bash
   docker ps --filter name=weatherquant-paper
   docker logs --tail 100 weatherquant-paper
   docker exec weatherquant-paper id -u   # 1000
   ```

2. **Fresh health**

   ```bash
   docker inspect --format '{{json .State.Health}}' weatherquant-paper
   docker exec weatherquant-paper python /usr/local/bin/healthcheck.py
   ```

   The probe prefers `/data/health/heartbeat.json`. The current engine writes its
   fresh heartbeat to `/data/paper/.paper.lock`, which the probe uses as a fallback.
   A native health file, once implemented by the engine, takes precedence. Health
   becomes unhealthy after 120 seconds without a heartbeat.

3. **Read-only API and persisted account**

   ```bash
   curl -fsS "http://${PAPER_API_LAN_IP}:8790/api/v1/paper/account"
   curl -fsS "http://${PAPER_API_LAN_IP}:8790/api/v1/paper/fills"
   curl -fsS "http://${PAPER_API_LAN_IP}:8790/api/v1/paper/health"
   ```

   Check that the opening capital is correct, the ledger is consistent, and any
   fills/refusals correspond to new Shadow LIVE ACCEPT decisions. No fills is a
   valid result when Shadow publishes no acceptable opportunity.

4. **Input mounts remain read-only**

   ```bash
   docker inspect --format '{{range .Mounts}}{{println .Source "->" .Destination "RW=" .RW}}{{end}}' \
     weatherquant-paper
   ```

   Only `/data` may be writable. `/shadow-decisions` and `/collector-raw` must be
   read-only. No collector or Shadow DuckDB path should appear.

5. **Isolation and resources**

   Confirm collector and Shadow are still running, their heartbeats continue to
   advance, and Paper stays within its `2g` memory and `1.0` CPU limits. Logs rotate
   at five 20 MB files.

## 6. Back up Paper state

Stop Paper before copying DuckDB. Copying a live database can produce an
inconsistent backup. Preserve a WAL as well when one exists.

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/mnt/user/appdata/weatherquant-paper/backups/$STAMP
mkdir -p "$BACKUP"
docker compose --env-file .env -f compose.paper.unraid.yaml stop paper
cp -a /mnt/user/appdata/weatherquant-paper/paper/paper.duckdb "$BACKUP/"
if [ -f /mnt/user/appdata/weatherquant-paper/paper/paper.duckdb.wal ]; then
  cp -a /mnt/user/appdata/weatherquant-paper/paper/paper.duckdb.wal "$BACKUP/"
fi
docker compose --env-file .env -f compose.paper.unraid.yaml start paper
```

Keep the backup outside any directory later used as a rollback source, and record
the `PAPER_SHA` that produced it alongside the files.

## 7. Roll back image and state

Use a previous full commit SHA whose `sha-<full sha>` image still exists. Schema
migrations are forward-only, so confirm that the older image supports the backup's
schema before proceeding.

1. Stop only Paper.
2. Move the current `paper.duckdb` (and WAL, if present) into a new safety-backup
   directory; do not delete it.
3. Copy the chosen stopped-state backup's `paper.duckdb` and matching WAL, if any,
   back to `/mnt/user/appdata/weatherquant-paper/paper/`.
4. Restore ownership with
   `chown -R 1000:1000 /mnt/user/appdata/weatherquant-paper/paper`.
5. Set `PAPER_SHA` to the previous released commit in the stack `.env`.
6. Pull and start the pinned image, then repeat the health/account checks.

```bash
docker compose --env-file .env -f compose.paper.unraid.yaml stop paper
# Move current state aside and restore the chosen backup as described above.
docker compose --env-file .env -f compose.paper.unraid.yaml pull
docker compose --env-file .env -f compose.paper.unraid.yaml up -d paper
curl -fsS "http://${PAPER_API_LAN_IP}:8790/api/v1/paper/account"
```

This changes only `weatherquant-next-paper`. It does not move collector `stable`,
Shadow `shadow-stable`, or any other image tag.

## 8. Upgrade and release notes

Releases are tagged `paper-vX.Y.Z`; the version must match `pyproject.toml`.
`paper-release.yml` tests and verifies the image before publishing immutable
`sha-<full sha>` and version tags, then moves only `paper-stable`. Development
`paper-ci.yml` builds but never pushes.

For an upgrade, make a stopped-state backup, set the new full `PAPER_SHA`, pull,
start, and repeat the first-hour checks. To disable Paper without changing its
saved state, stop its stack or set `WQ_PAPER_ENABLED=false`; LIVE collection is
independent and continues either way.
