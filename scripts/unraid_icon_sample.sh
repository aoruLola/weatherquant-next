#!/usr/bin/env bash
# Read-only Unraid host sample. Write stdout to a new evidence file.
set -euo pipefail
export LC_ALL=C

seconds=${1:-120}
root=${2:-/}
[[ "$seconds" =~ ^[1-9][0-9]*$ ]] || { echo "duration must be positive whole seconds" >&2; exit 2; }
root=${root%/}
plugin="$root/usr/local/emhttp/plugins/dynamix.docker.manager"
log="$root/var/log/nginx/error.log"
digest() {
    if [[ -f "$1" ]]; then sha256sum "$1" | cut -d' ' -f1; else echo missing; fi
}

printf 'host\t%s\n' "$(hostname)"
printf 'version\t'; cat "$root/etc/unraid-version"
printf 'boot_id\t'; cat "$root/proc/sys/kernel/random/boot_id"
printf 'client_sha256\t%s\n' "$(digest "$plugin/include/DockerClient.php")"
printf 'containers_sha256\t%s\n' "$(digest "$plugin/include/DockerContainers.php")"
printf 'icon_sha256\t%s\n' "$(digest "$plugin/images/question.png")"

# Only count bytes appended inside this observation window. Never tail the
# whole log on a host whose tmpfs may already be near capacity.
read -r identity start_size < <(stat -Lc '%d:%i %s' "$log")
start=$(date +%s)
sleep "$seconds"
end=$(date +%s)
read -r end_identity end_size < <(stat -Lc '%d:%i %s' "$log")
printf 'start\t%s\nend\t%s\n' "$start" "$end"
if [[ "$identity" != "$end_identity" || "$end_size" -lt "$start_size" ]]; then
    echo 'nginx error log rotated or truncated; take another sample' >&2
    exit 2
fi
bytes=$((end_size - start_size))
printf 'appended_bytes\t%s\n' "$bytes"
if (( bytes > 16777216 )); then
    echo 'more than 16 MiB appended; byte growth recorded, line counts unavailable' >&2
    exit 2
fi
dd if="$log" iflag=skip_bytes,count_bytes skip="$start_size" count="$bytes" bs=65536 status=none |
    awk '{n++; if (index($0,"/plugins/dynamix.docker.manager/images/question.png")) icons++}
         END {printf "error_lines\t%d\nicon_error_lines\t%d\n", n, icons}'
printf 'complete\t1\n'
