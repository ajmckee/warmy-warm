# Laptop development lab

Requires Docker with Compose v2+ and a running daemon. The image is pinned to
`ghcr.io/home-assistant/home-assistant:2026.3.0`, matching the pytest dependency.
Run from the repository root:

```sh
sh dev/up.sh
```

Open **http://127.0.0.1:8123** and create a **local lab account**. Skip discovery
and optional location/provider setup. No live HA token, heat-pump connection,
Mosquitto, MQTT credentials, or weather account is used. The first image pull
and Python dependency installation need internet access; the lab then runs locally.

`.dev-ha/` is a separate gitignored config directory containing the database,
onboarding, local credentials, and `.storage`. The startup script copies the
tracked template only when `configuration.yaml` does not exist. Both custom
integrations are mounted read-only; restarting picks up source edits. Only port
8123 on the laptop loopback interface is published. There is no host networking,
privileged mode, hardware mount, MQTT service, or automatic network discovery.
The simulator lives outside the production integration; install only
`custom_components/thermal_planner` on a real HA installation.

Useful commands (from the repository root):

```sh
# Prepare the config without starting Docker
sh dev/up.sh --prepare-only
# Validate Compose and HA config
docker compose -f compose.dev.yaml config --quiet
docker compose -f compose.dev.yaml run --rm homeassistant python -m homeassistant --script check_config --config /config
# Logs, reload code, stop (all preserve the lab config)
docker compose -f compose.dev.yaml logs --tail=100 homeassistant
docker compose -f compose.dev.yaml restart homeassistant
docker compose -f compose.dev.yaml down
```

Do not point these mounts at a live HA configuration. To start a completely new
lab, stop it and rename `.dev-ha/` before running the startup command again.

## Phase 1 acceptance on a laptop

1. Start the lab. In **Settings → Devices & services → Entities**, check
   `climate.fake_house_heat_pump`, `sensor.fake_house_radiator`,
   `sensor.fake_house_ufh`, `sensor.fake_house_bathroom`, and
   `sensor.fake_house_outdoor`.
2. Add **Thermal Planner** through Devices & services. Select the fake outdoor
   sensor; leave weather empty. On **Rooms**, choose **Add room** and repeat for
   radiator, UFH and bathroom using their corresponding sensors. Floors are
   optional; for the weighted-floor check, first create Ground in HA's
   **Settings → Areas, labels & zones → Floors**, then choose it for these rooms.
   In **More room settings**, set UFH heating type to Underfloor, radiator/UFH
   floor weights to 2 and 1, and bathroom weight to 0. Set the bathroom minimum
   to 19°C and leave inclusion in
   the first-room-to-cool result enabled. Choose **Save configuration**.
3. In Developer tools → Actions, call `climate.turn_off` on
   `climate.fake_house_heat_pump`. This is a manual simulator command; Phase 1
   Thermal Planner issues no climate writes. Wait at least six real minutes for
   Recorder history, then open **Thermal Planner** in the sidebar and choose a past window of at
   least five minutes under **Create a report**. Select **Analyse temperatures**. Inspect rooms, floor, bathroom, coverage,
   and the optional authenticated CSV export. Select a completed history window, ending
   before the present. Return the fake climate to heat with `climate.turn_on`.
4. For a missing-input check, use Developer tools → States to set the fake
   outdoor sensor to `unavailable`, and replay a window covering the gap.
   Disable `auto_tick` in `.dev-ha/configuration.yaml` and restart first if you
   want the gap to persist. Missing outdoor data must withhold normalization
   while indoor results remain available. Restore `auto_tick: true` afterward.
5. Run the deterministic suite below. It covers precise four-hour metrics,
   independent of today's date or how long the container has been running.

The template advances five simulated minutes every five real seconds (60×).
Recorder timestamps remain **real time**: live reports therefore show accelerated
rates, and short windows can have limited coverage at the analysis five-minute
cadence. Do not compare those rates to the fixture's °C/h expectations. For
real-time runs, set `tick_seconds: 60` and `step_minutes: 1`, then restart.

For deterministic manual stepping, set `auto_tick: false`, restart, and call:

```yaml
action: fake_house.step
data:
  minutes: 60
```

`fake_house.reset` resets mode to heat, target to 23°C and temperatures to their
initial values. Step/reset change only the simulator. Manual steps do not create
backdated Recorder samples. Normal commands and steps persist the simulator's
mode, target and temperatures. Restarting while off **keeps it off**; there is no
simulator auto-resume that could conceal a planner recovery defect.

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check custom_components dev tests
.venv/bin/ruff format --check custom_components dev tests
```

`tests/conftest.py` provides fresh `fake_house`, `simulated_cooldown`, and
`fixture` inputs. The generated cooldown starts at 2026-01-01T00:00:00Z, uses
five-minute steps, and has no random inputs or wall-clock dependency. It tests
the same toy model used in the lab, including radiator/UFH response, bathroom
guard breach, weighted floors, and a real HA climate-service round trip. Existing
tests also exercise actual SQLite Recorder history and replay persistence. The
simulator tests disable timers and use temporary config directories.

Verification completed against HA 2026.3.0: 118 pytest tests pass, Ruff lint and
format checks pass, Compose and HA configuration validation pass, the Container
serves HTTP 200 before and after restart, and Recorder contains the climate and
all four fake temperature entities. Docker mount inspection confirms both source
mounts are read-only. HA also validates the simulator's action descriptions.
The automated controller gate in `tests/test_control.py` also verifies pause,
abort, deadlines, manual takeover, restore readback, bounded retries and actual
HA restart restoration against the simulated climate. Chromium checks use the
production panel asset with a mocked local transport; a full authenticated
container frontend walkthrough remains a manual acceptance check.

## Phase 2 gate before real heat-pump commissioning

Controlled measurements are available under **Measurements** on the sidebar
page for administrators. Select the registered fake-house source and its room
sensors for laptop acceptance. Real sources require separate on-site commissioning.
The lab supports a 1–60 real minute pause,
Stop / retry restore, a persisted control journal, startup reconciliation,
minimum/missing/stale guards, and a visible last-run event log.

`tests/test_control.py` passes the automated scenarios below using controlled time,
real HA climate services, temporary HA storage, service/readback failures
and fresh HA instances for restart tests. The separate simulator tests prove
that the simulated device itself stays OFF over restart. The controller, rather
than a default-to-HEAT simulator, must restore it.

For a manual container check, configure only fake-house room sensors and safe
minimums. Put the fake climate in Heat at a non-default target (for example
24°C), open the sidebar page, and start a lab pause. Stop it and check Heat/24°C
readback. Start again, step the fake house until a minimum is reached, and check
the automatic abort/reason. Start again and use the restart command below while
OFF. Check Heat/24°C and a cleared active journal after startup.

Do not set pause cutoffs in simulated minutes: the controller uses actual UTC
deadlines. The accelerated thermal model can reach a guard floor before the
real-time deadline. Real sources require explicit on-site commissioning; do not enable it for laptop acceptance.

| Scenario | Required evidence |
|---|---|
| Bounded pause | Start from heat at a non-default target, capture settings and deadline in the persisted journal before OFF, read back OFF, and observe cooling. Overlapping starts are rejected. |
| Completion/user abort | Stop the planner run early and also let a deadline expire; both restore the captured mode/target, verify readback, then clear ownership. Repeat Stop to prove idempotence. |
| Guard abort | Step the bathroom below its floor; separately make a guard unavailable/stale. Each must abort promptly, restore, and surface the reason. |
| Restart while paused | With the simulator still OFF, run `docker compose -f compose.dev.yaml restart homeassistant`. Reconcile the persisted journal and verify the original mode/target return; the simulator itself must not perform the restore. |
| Crash boundaries | Inject failure before OFF, after OFF was accepted but before acknowledgment was saved, and during restoration. Reconcile safely on restart and retain the journal until restore readback succeeds. |
| Failed restore/unavailable entity | Inject service failures and unavailable readback; verify bounded retries, visible persistent failure, retained journal, and successful reconciliation when the entity returns. |
| External takeover | Change the fake climate target/mode manually while paused; preserve that change and release planner ownership rather than overwriting it. |

Record HA/image version, commands, journal transitions and final readbacks for
each case. Automated simulated control checks pass; complete the manual
container walkthrough before real-device commissioning as a separate step with someone present.
HA cannot restore a device while HA itself is offline.


## Reports page browser verification

The optional browser dependency is separate from runtime and normal pytest:

```sh
uv pip install --python .venv/bin/python -e '.[test,test-ui]'
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/browser_smoke.py
```

To use an installed browser instead, pass `--chrome /path/to/chrome`. The smoke
check starts an ephemeral loopback HTTP server and injects a fake `hass.callWS`
transport. It exercises the real panel JavaScript, chart/table rendering,
comparison windows, date-validation messages, analysis, CSV export, lab
start/stop, HTML escaping and mobile overflow. It writes desktop/mobile
screenshots under `/tmp/thermal-planner-browser`; it uses no HA credentials and
issues no real device commands. API authorization and actual controller behavior
are exercised separately by pytest.


## Remaining-phase laptop walkthrough (no MQTT)

Start the pinned container with this exact command from the repository root:

```sh
sh dev/up.sh
```

Open http://127.0.0.1:8123 and complete the separate development HA onboarding.
Configure Thermal Planner with the fake-house room sensors, outdoor sensor and
`climate.fake_house_heat_pump`. Keep one Main zone; label rooms radiator or UFH.
Optional lab telemetry is exposed as native sensor, binary_sensor, number and
weather entities by `fake_house`. Select these through HA selectors; no live
credentials or broker are used. Use `number.fake_house_flow_target` as both direct
setter and readback only when testing flow trials, with explicit lab bounds
30–50°C and maximum change 2°C. These are **simulator examples**, not pump settings.

1. Run a bounded cooldown, abort it and verify Heat restoration; repeat across a
   container restart. Automated HA tests additionally inject missing readbacks,
   failures, manual takeover and crash boundaries.
2. Start a normal-operation observation: it sends no control commands. Use a
   completed measurement’s **View timeline** to inspect cooldown/recovery and
   optional energy. Configure a future local campaign time, count and end date.
3. Select the fake weather entity, then use **Load synthetic learning demo** in
   Learning. The demo is distinct from observed simulator records, deterministic,
   labelled synthetic and permitted only for the selected fake-house source.
4. Fit room models, inspect holdout errors, refresh the forecast and plan, and
   compare baseline versus selected trajectories. Apply once or enable Auto only
   in the fake house; Stop cancels scheduling and restores any owned command.
5. Test DHW/defrost using `fake_house.set_conditions`; these states contaminate
   learning evidence and prevent/abort source measurements rather than treating
   them as ordinary space heating.

The browser smoke test uses the production page with local mocked transport;
pytest exercises real HA entity/service/registry/storage APIs. Neither proves
hardware behavior, HAOS deployment, or an authenticated browser walkthrough of
your installation. Those remain installation acceptance steps.

Marketplace packaging, HA 2026.11 compatibility checks and publication steps are documented in [Publishing](../docs/PUBLISHING.md).
