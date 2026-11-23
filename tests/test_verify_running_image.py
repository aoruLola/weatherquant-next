"""verify_running_image: the container must be the build it claims to be.

Docker is not installed on this box, so every case runs against fixture
``docker inspect`` JSON. The check is a pure function for exactly this reason:
a tag comparison is the kind of thing that silently rots if it can only be
exercised on a NAS.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_running_image.py"
SHA = "5410c75d584771b4477de07f4b1d7b83b764bfe1"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"


def _load():
    spec = importlib.util.spec_from_file_location("wq_verify_running_image", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verify():
    return _load()


def _document(
    *,
    image: str | None = None,
    git_sha: str | None = SHA,
    image_tag: str | None = SHA,
    running: bool = True,
) -> dict:
    environment = []
    if git_sha is not None:
        environment.append(f"GIT_SHA={git_sha}")
    if image_tag is not None:
        environment.append(f"IMAGE_TAG={image_tag}")
    return {
        "Name": "/weatherquant-collector",
        "Config": {
            "Image": image or f"ghcr.io/aorulola/weatherquant-next:sha-{SHA}",
            "Env": ["TZ=UTC", *environment],
        },
        "State": {"Running": running, "Status": "running" if running else "exited"},
        "RepoDigests": [f"ghcr.io/aorulola/weatherquant-next@sha256:{'a' * 64}"],
    }


def test_a_matching_container_passes(verify) -> None:
    verdict = verify.check_container(_document(), SHA)
    assert verdict.ok
    assert verdict.problems == ()
    assert verdict.expected_tag == f"sha-{SHA}"
    assert verdict.actual_tag == f"sha-{SHA}"


def test_the_image_may_also_report_the_tag_form_of_image_tag(verify) -> None:
    """Some images record IMAGE_TAG as `sha-<commit>` rather than bare."""
    assert verify.check_container(_document(image_tag=f"sha-{SHA}"), SHA).ok


def test_a_floating_stable_tag_is_a_mismatch(verify) -> None:
    document = _document(image="ghcr.io/aorulola/weatherquant-next:stable")
    verdict = verify.check_container(document, SHA)
    assert not verdict.ok
    assert any("image tag is 'stable'" in problem for problem in verdict.problems)


def test_a_different_commit_tag_is_a_mismatch(verify) -> None:
    document = _document(image=f"ghcr.io/aorulola/weatherquant-next:sha-{OTHER_SHA}")
    verdict = verify.check_container(document, SHA)
    assert not verdict.ok
    assert any(OTHER_SHA in problem for problem in verdict.problems)


def test_a_digest_reference_has_no_sha_tag(verify) -> None:
    document = _document(image=f"ghcr.io/aorulola/weatherquant-next@sha256:{'b' * 64}")
    verdict = verify.check_container(document, SHA)
    assert not verdict.ok
    assert any("image tag is '(none)'" in problem for problem in verdict.problems)


def test_wrong_git_sha_environment_is_a_mismatch(verify) -> None:
    verdict = verify.check_container(_document(git_sha=OTHER_SHA), SHA)
    assert not verdict.ok
    assert any("GIT_SHA" in problem for problem in verdict.problems)


def test_missing_environment_stamps_are_a_mismatch(verify) -> None:
    verdict = verify.check_container(_document(git_sha=None, image_tag=None), SHA)
    assert not verdict.ok
    assert any("GIT_SHA is '(unset)'" in problem for problem in verdict.problems)
    assert any("IMAGE_TAG is '(unset)'" in problem for problem in verdict.problems)


def test_a_stopped_container_is_a_mismatch(verify) -> None:
    verdict = verify.check_container(_document(running=False), SHA)
    assert not verdict.ok
    assert "the container is not running" in verdict.problems


def test_the_one_element_list_from_docker_inspect_is_accepted(verify) -> None:
    assert verify.check_container([_document()], SHA).ok


def test_zero_or_multiple_documents_are_rejected(verify) -> None:
    with pytest.raises(ValueError):
        verify.check_container([], SHA)
    with pytest.raises(ValueError):
        verify.check_container([_document(), _document()], SHA)


def test_a_blank_sha_is_rejected(verify) -> None:
    with pytest.raises(ValueError):
        verify.check_container(_document(), "  ")


def test_cli_exit_codes(tmp_path: Path, verify, capsys) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps([_document()]), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([_document(git_sha=OTHER_SHA)]), encoding="utf-8")

    assert verify.main(["--inspect", str(good), "--sha", SHA]) == 0
    assert "RESULT: MATCH" in capsys.readouterr().out

    assert verify.main(["--inspect", str(bad), "--sha", SHA]) == 1
    assert "RESULT: MISMATCH" in capsys.readouterr().out

    assert verify.main(["--inspect", str(tmp_path / "missing.json"), "--sha", SHA]) == 2
