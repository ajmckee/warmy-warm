# Release notes

## 0.3.1 — prepared for first public release

### Before using heating controls

This is a custom integration maintained independently of Home Assistant. Start with temperature reports and advisory mode. Controlled measurements, Apply once and Auto require explicit commissioning with someone present. Home Assistant cannot restore a device while it is offline. Real heat-pump and HAOS installation acceptance are still required.

### Included

- Four dashboard pages: Reports, Measurements, Learning and Heating plan.
- Room setup with optional native HA floors and circuit labels; one shared heat pump with multiple zones and independent radiator/UFH room types.
- Historical temperature reports, saved results and optional CSV export.
- Read-only observations, bounded cooldown/recovery measurements, scheduled campaigns and persisted restoration checks.
- Validated room models and weather-based advisory plans; optional bounded direct-flow trials and explicit automatic control.
- Optional humidity, comfort estimates and scoped electrical/thermal energy readings.
- Marketplace/HACS metadata for `ajmckee/warmy-warm`, MIT license, local brand images and repository validation workflows.
- A separate, credential-free development simulator. No MQTT integration is needed.

### Compatibility and updates

Minimum Home Assistant: 2026.3.0. Marketplace installation is available on HA 2026.11 and later; older supported installations use HACS. Restart Home Assistant after updating. Existing local configuration entries and saved reports are retained; user data lives in HA storage outside the integration folder.

No release has been published by these preparation changes. Confirm passing validation runs and complete the checks in `docs/PUBLISHING.md` before creating the GitHub release.
