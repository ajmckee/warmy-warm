"""Check local publication metadata without network access or Home Assistant."""

import argparse
import json
import re
import struct
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "thermal_planner"


def check(root=ROOT, tag=None, repository=None):
    """Return actionable errors; do not allow an incomplete release to look ready."""
    errors = []
    component = root / "custom_components" / DOMAIN
    manifest = json.loads((component / "manifest.json").read_text())
    hacs = json.loads((root / "hacs.json").read_text())
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = manifest.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:b\d+)?", version):
        errors.append("Set a valid release version in manifest.json.")
    if project["version"] != version or (tag and tag != version):
        errors.append("Release tag, manifest.json and pyproject.toml versions must match exactly.")
    if manifest.get("domain") != DOMAIN:
        errors.append("Integration domain must match its folder.")
    folders = sorted(
        p.name for p in component.parent.iterdir() if p.is_dir() and not p.name.startswith("_")
    )
    if folders != [DOMAIN]:
        errors.append("Keep exactly one published integration under custom_components/.")
    docs = manifest.get("documentation", "")
    match = re.fullmatch(r"https://github.com/([\w.-]+/[\w.-]+)(?:/.*)?", docs)
    if not match or match[1] in ("OWNER/REPOSITORY", "home-assistant/core"):
        errors.append(
            "Point manifest documentation to Thermal Planner's own public GitHub repository."
        )
    elif repository and match[1].lower() != repository.lower():
        errors.append("Manifest documentation does not match the publishing GitHub repository.")
    if not match or manifest.get("issue_tracker") != f"https://github.com/{match[1]}/issues":
        errors.append("Set issue_tracker to this repository's GitHub Issues URL.")
    if not manifest.get("codeowners") or not all(
        re.fullmatch(r"@[a-zA-Z0-9][a-zA-Z0-9-]*", owner) and owner != "@OWNER"
        for owner in manifest["codeowners"]
    ):
        errors.append("Set codeowners to the actual maintainer's @GitHub username.")
    if hacs.get("name") != "Thermal Planner" or hacs.get("homeassistant") != "2026.3.0":
        errors.append("Keep hacs.json's name and tested minimum HA version consistent.")
    if hacs.get("zip_release") or hacs.get("content_in_root"):
        errors.append(
            "This release installs from custom_components/thermal_planner in the repository."
        )
    if not (root / "LICENSE").is_file():
        errors.append("Add the maintainer's chosen license before publishing.")
    for filename, size in (("icon.png", 256), ("icon@2x.png", 512)):
        path = component / "brand" / filename
        if not path.is_file():
            errors.append(f"Missing local brand image {filename}.")
            continue
        data = path.read_bytes()
        if (
            len(data) < 24
            or data[:8] != b"\x89PNG\r\n\x1a\n"
            or struct.unpack(">II", data[16:24]) != (size, size)
        ):
            errors.append(f"{filename} must be a {size}×{size} PNG.")
    translations = json.loads((component / "translations/en.json").read_text())
    if not translations.get("config") or not translations.get("options"):
        errors.append("Ship complete English setup and options translations.")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Expected release tag, for example 0.3.1")
    parser.add_argument("--repository", help="Expected GitHub owner/repository")
    args = parser.parse_args()
    errors = check(tag=args.tag, repository=args.repository)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(1)
    print(
        "Local release metadata checks passed. GitHub HACS/hassfest and installation acceptance are separate checks."
    )


if __name__ == "__main__":
    main()
