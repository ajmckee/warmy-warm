# Contributing

Start with the [development lab guide](dev/README.md). The fake house and deterministic fixtures need no live heating, Home Assistant login, MQTT broker or credentials.

Run the pytest suite, Ruff checks and browser smoke check before submitting changes. Keep all runtime files under `custom_components/thermal_planner/`; the fake house belongs under `dev/` and must never be installed with the production integration. Keep room names and device identities configurable.

Changes to heating control must cover restoration, failed/missing readbacks, manual overrides and restart boundaries with simulator tests. Never use a real heat pump as a CI target. Do not include live HA exports, `.storage`, tokens or credentials in issues, screenshots or commits.

Publication requirements and validation commands are in [Publishing](docs/PUBLISHING.md). Maintainers choose release versions and publish releases after reviewing the results.
