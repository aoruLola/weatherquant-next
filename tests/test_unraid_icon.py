import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_unraid_icon", ROOT / "scripts/verify_unraid_icon.py")
assert spec and spec.loader
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.fixture
def evidence(monkeypatch):
    png = b"\x89PNG\r\n\x1a\nfixture"
    icon = hashlib.sha256(png).hexdigest()
    monkeypatch.setitem(check.OFFICIAL, "icon_sha256", icon)
    prior = dict(check.OFFICIAL, host="tower", version="fixture", boot_id="before", start=0, end=120,
                 appended_bytes=100, error_lines=10, icon_error_lines=10, errors_per_second=10/120)
    after = dict(prior, boot_id="after", start=180, end=300, appended_bytes=0, error_lines=0,
                 icon_error_lines=0, errors_per_second=0)
    browser = dict(origin="http://tower", page="/Docker", start=190, end=251, hidden=False, requests=0,
                   dropped=0, image_base64=base64.b64encode(png).decode())
    return prior, after, browser


def test_report_requires_independent_host_browser_and_log_evidence(evidence):
    report = check.compare(*evidence, "http://tower")
    assert report["passed"] and all(report["checks"].values())
    assert "image_base64" not in report["browser"]


@pytest.mark.parametrize(("index", "change", "gate"), [
    (1, {"boot_id": "before"}, "same_host_rebooted"),
    (1, {"client_sha256": "other"}, "official_files_after_boot"),
    (2, {"origin": "http://other"}, "docker_page_on_expected_origin"),
    (2, {"image_base64": "PGh0bWw+"}, "served_png_matches_host"),
    (2, {"hidden": True}, "visible_browser_sample"),
    (2, {"dropped": 3}, "visible_browser_sample"),
    (2, {"end": 305}, "visible_browser_sample"),
    (2, {"requests": 5}, "no_repeated_icon_requests"),
    (0, {"errors_per_second": 0}, "nginx_error_rate_fell"),
    (1, {"icon_error_lines": 1}, "no_icon_errors_after_boot"),
])
def test_missing_gate_never_passes(evidence, index, change, gate):
    evidence[index].update(change)
    report = check.compare(*evidence, "http://tower")
    assert not report["passed"] and not report["checks"][gate]


@pytest.mark.parametrize("rotate", [False, True])
def test_host_sampler_counts_only_new_lines_and_refuses_rotation(tmp_path, rotate):
    bash = shutil.which("bash") if os.name != "nt" else str(Path(os.environ["PROGRAMFILES"]) / "Git/bin/bash.exe")
    for folder in ("etc", "proc/sys/kernel/random", "var/log/nginx", "bin"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "etc/unraid-version").write_text("version=fixture\n")
    (tmp_path / "proc/sys/kernel/random/boot_id").write_text("boot\n")
    log = tmp_path / "var/log/nginx/error.log"
    log.write_text("historic /plugins/dynamix.docker.manager/images/question.png\n")
    sleep = tmp_path / "bin/sleep"
    sleep.write_text("#!/bin/sh\n" + ("mv var/log/nginx/error.log var/log/nginx/old\n" if rotate else "")
                     + "printf 'nginx /plugins/dynamix.docker.manager/images/question.png\\nother error\\n' "
                     ">> var/log/nginx/error.log\n/bin/sleep 1\n", newline="\n")
    sleep.chmod(0o755)
    args = [bash, "--noprofile", "--norc", "-c", 'PATH="$PWD/bin:$PATH"; bash "$1" 1 .', "--",
            (ROOT / "scripts/unraid_icon_sample.sh").as_posix()]
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, check=False)
    capture = tmp_path / "sample.tsv"
    capture.write_text(result.stdout)
    if rotate:
        assert result.returncode == 2 and "rotated" in result.stderr
        with pytest.raises(ValueError, match="did not finish"):
            check.read_sample(capture)
    else:
        assert result.returncode == 0, result.stderr
        sample = check.read_sample(capture)
        assert sample["error_lines"] == 2 and sample["icon_error_lines"] == 1
        assert sample["icon_sha256"] == "missing"


def test_cli_rejects_partial_sample(tmp_path):
    path = tmp_path / "partial.tsv"
    path.write_text("boot_id\tbefore\n")
    browser = tmp_path / "browser.json"
    browser.write_text(json.dumps({}))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_unraid_icon.py"), "--before", str(path),
                             "--after", str(path), "--browser", str(browser), "--origin", "http://tower"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2 and "incomplete evidence" in result.stderr
