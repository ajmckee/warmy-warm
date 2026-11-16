"""A release must not pass with guessed identity or inconsistent versions."""

import json
from pathlib import Path
from shutil import copy2

import pytest

from dev.check_release import check

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def publication(tmp_path):
    files = [
        "hacs.json",
        "pyproject.toml",
        "custom_components/thermal_planner/manifest.json",
        "custom_components/thermal_planner/translations/en.json",
        "custom_components/thermal_planner/brand/icon.png",
        "custom_components/thermal_planner/brand/icon@2x.png",
    ]
    for filename in files:
        target = tmp_path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(ROOT / filename, target)
    manifest = tmp_path / "custom_components/thermal_planner/manifest.json"
    data = json.loads(manifest.read_text())
    data.update(
        documentation="https://github.com/example/thermal-planner",
        issue_tracker="https://github.com/example/thermal-planner/issues",
        codeowners=["@example"],
    )
    manifest.write_text(json.dumps(data))
    (tmp_path / "LICENSE").write_text("Test-only license fixture")
    return tmp_path


def test_complete_release_metadata(publication):
    assert check(publication, tag="0.3.1", repository="example/thermal-planner") == []


@pytest.mark.parametrize(
    "tag,repository,expected",
    [
        ("0.3.0", "example/thermal-planner", "versions must match"),
        ("0.3.1", "different/repository", "does not match"),
    ],
)
def test_reject_wrong_release_identity(publication, tag, repository, expected):
    assert any(expected in error for error in check(publication, tag, repository))


def test_reject_missing_publication_details(publication):
    path = publication / "custom_components/thermal_planner/manifest.json"
    data = json.loads(path.read_text())
    data.update(
        codeowners=[], issue_tracker="", documentation="https://github.com/home-assistant/core"
    )
    path.write_text(json.dumps(data))
    (publication / "LICENSE").unlink()
    errors = check(publication)
    assert len(errors) == 4
    assert any("actual maintainer" in error for error in errors)
    assert any("chosen license" in error for error in errors)


def test_reject_second_installed_integration_and_broken_brand(publication):
    (publication / "custom_components/fake_house").mkdir()
    (publication / "custom_components/thermal_planner/brand/icon.png").write_bytes(b"broken")
    errors = check(publication)
    assert any("exactly one" in error for error in errors)
    assert any("256×256" in error for error in errors)
