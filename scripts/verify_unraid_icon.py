"""Compare WQN-53 host samples with a visible, signed-in Docker page observation."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

# Unraid webgui 7.3.3 (31c22f83), also identical at d3898259 and 28b893c8.
OFFICIAL = {
    "client_sha256": "62cf5f76d15db27228baa185098fa2496168544f648301e3b3b88265ddc74f2e",
    "containers_sha256": "5cf208554d900896fffda3ef71756cde7ca91bda56dcaf2c6ecb5f39bdee7404",
    "icon_sha256": "85b3ab5476c3cdcdfac6f1b31a0da98fe582fff692b05ba597594cf49b6e721b",
}


def read_sample(path: Path) -> dict:
    sample = dict(line.split("\t", 1) for line in path.read_text(encoding="utf-8-sig").splitlines())
    if sample.get("complete") != "1":
        raise ValueError("host sample did not finish")
    for field in ("host", "version", "boot_id", *OFFICIAL):
        if not sample.get(field):
            raise ValueError(f"host sample missing {field}")
    for field in ("start", "end", "error_lines", "icon_error_lines", "appended_bytes"):
        sample[field] = int(sample[field])
    duration = sample["end"] - sample["start"]
    if duration <= 0 or not 0 <= sample["icon_error_lines"] <= sample["error_lines"] <= sample["appended_bytes"]:
        raise ValueError("invalid log observation window or counts")
    sample["errors_per_second"] = sample["error_lines"] / duration
    sample["icon_errors_per_second"] = sample["icon_error_lines"] / duration
    return sample


def compare(before: dict, after: dict, browser: dict, origin: str) -> dict:
    image = base64.b64decode(browser["image_base64"], validate=True)
    checks = {
        "same_host_rebooted": bool(before["host"] and before["host"] == after["host"]
                                   and before["boot_id"] and after["boot_id"] and before["boot_id"] != after["boot_id"]
                                   and before["end"] <= after["start"]),
        "official_files_after_boot": all(after[field] == digest for field, digest in OFFICIAL.items()),
        "docker_page_on_expected_origin": browser["origin"] == origin and browser["page"].rstrip("/") == "/Docker",
        "served_png_matches_host": image.startswith(b"\x89PNG\r\n\x1a\n")
        and hashlib.sha256(image).hexdigest() == after["icon_sha256"],
        "visible_browser_sample": browser["hidden"] is False and browser["dropped"] == 0
        and browser["end"] - browser["start"] >= 60
        and after["start"] <= browser["start"] <= browser["end"] <= after["end"],
        "no_repeated_icon_requests": browser["requests"] == 0,
        "host_windows_at_least_one_minute": min(before["end"] - before["start"], after["end"] - after["start"]) >= 60,
        "nginx_error_rate_fell": after["errors_per_second"] < before["errors_per_second"],
        "no_icon_errors_after_boot": after["icon_error_lines"] == 0,
    }
    return {
        "passed": all(checks.values()), "checks": checks, "before": before, "after": after,
        "browser": {k: browser[k] for k in ("origin", "page", "start", "end", "hidden", "dropped", "requests")},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--origin", required=True, help="expected WebUI origin, e.g. http://tower")
    args = parser.parse_args()
    try:
        report = compare(read_sample(args.before), read_sample(args.after),
                         json.loads(args.browser.read_text(encoding="utf-8-sig")), args.origin.rstrip("/"))
    except (KeyError, ValueError, TypeError, OSError) as exc:
        parser.exit(2, f"incomplete evidence: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
