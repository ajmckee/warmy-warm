# Preparing and publishing Thermal Planner

Thermal Planner is distributed as a **custom integration** in the Home Assistant Marketplace, with the same repository layout used by HACS. It is not a separate Supervisor app. The embedded dashboard and its assets install with the integration.

This guide follows the [official custom integration guide](https://contributors.home-assistant.io/marketplace/guides/integrations/), [repository requirements](https://contributors.home-assistant.io/marketplace/reference/repository-structure/) and [validation requirements](https://contributors.home-assistant.io/marketplace/reference/validation/).

## Repository setup

Publication identity is configured for [ajmckee/warmy-warm](https://github.com/ajmckee/warmy-warm), maintained by **@ajmckee**, under the **MIT license**. The manifest, package metadata, README install badge and license now use these confirmed details. The local `origin` remote is configured for this repository.

Before pushing:

1. Create or confirm the **public** GitHub repository `ajmckee/warmy-warm` and enable Issues.
2. Set the description to: **Understand how your home cools and reheats, with room reports and guarded heating plans for Home Assistant.**
3. Add topics such as `home-assistant`, `custom-integration`, `hacs`, `heat-pump`, `underfloor-heating` and `thermal-planner`.
4. Confirm that the [Issues page](https://github.com/ajmckee/warmy-warm/issues) and repository README are publicly accessible after pushing.


The only folder immediately below `custom_components` is `thermal_planner`. The fake house lives under `dev/custom_components` and is never installed by the Marketplace. User data is in HA `.storage`, so `persistent_directory` is unnecessary. `zip_release` is deliberately unset: installation uses the versioned repository files, including the bundled frontend, English translations and brand PNGs.

## Validate before release

```sh
.venv/bin/python dev/check_release.py --tag 0.3.2 --repository ajmckee/warmy-warm
.venv/bin/python -m pytest -q
.venv/bin/ruff check custom_components dev tests
.venv/bin/ruff format --check custom_components dev tests
```

The local metadata check verifies the confirmed repository identity, maintainer, license, version and package layout. It does not replace the official validators.

After pushing, wait for all GitHub Actions:

- **Marketplace validation / hacs:** must pass without ignored checks. It also checks remote repository properties such as description, topics, Issues and branding.
- **Marketplace validation / hassfest:** official manifest, translation and integration validation.
- **Tests:** HA 2026.3.0 and 2026.11.0 regression tests, lint, metadata checks, and the browser smoke test.

The weekly validation schedule watches for upstream validation changes. Dependabot updates GitHub Actions references. The minimum in `hacs.json` is 2026.3.0; do not lower it without testing. The development Container remains pinned to 2026.3.0.

Before calling the release installation-ready, install the integration from the repository into a clean HA instance. Verify setup, the four dashboard tabs, the local brand image, update/restart behavior and uninstall/reinstall. Complete HAOS installation acceptance and on-site source commissioning separately. Simulator and mocked browser checks do not establish actual heat-pump behavior.

## Prepare the release

Use version **0.3.2** consistently in `manifest.json`, `pyproject.toml` and the release tag. For future frontend changes, update the module URL in `panel.py` so existing browsers fetch the new script. Read and edit `CHANGELOG.md` before using it as release notes.

Once checks pass, commit the reviewed files, push them to the selected public repository and create a **full GitHub release** tagged `0.3.2`. A tag by itself is insufficient. Use the corresponding changelog text as the release body. GitHub publication and catalog submission are separate from this local preparation; neither has been performed here.

For an initial trial, mark the release as a prerelease and tell testers to enable prereleases. A catalog submission requires a regular published release and passing validation links. Do not advertise live-hardware verification until the relevant on-site checks have actually passed.

## Install from a link first

Users can add the public repository through a Marketplace repository link before it is in the catalog. On older supported HA releases, they can add it in HACS as a custom repository of type Integration. The [official catalog guide](https://contributors.home-assistant.io/marketplace/guides/catalog/) explains both routes and the badge formats.

Then users install Thermal Planner and add it through Settings → Devices & services. Updates require a Home Assistant restart. Marketplace/HACS installs the integration files, not the development Docker setup.

## Submit to the shared catalog later

The owner or a major contributor can submit a pull request to [hacs/default](https://github.com/hacs/default), adding `ajmckee/warmy-warm` alphabetically to its `integration` list. Fill in its template with the regular release URL and passing HACS and hassfest run links. Do not ignore failing checks. Catalog review is external and may take time; an accepted submission is what makes the integration discoverable without a custom repository link.

## Local preparation results

- 109 tests pass on HA 2026.3.0 and independently on HA 2026.11.0.
- Official hassfest container validation passes with zero invalid integrations and no warnings.
- Desktop/mobile browser smoke checks and Ruff lint/format checks pass.
- Publication metadata is configured for `ajmckee/warmy-warm`, `@ajmckee` and MIT; local release validation passes for tag `0.3.2`.
- HACS remote validation, public GitHub release and catalog submission have not been performed by this preparation.

These results cover repository preparation and simulator behavior. Clean Marketplace installation, HAOS acceptance and real source commissioning remain separate acceptance checks.
