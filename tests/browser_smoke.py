"""Optional real-browser check with local report fixtures, no HA login or devices.

Run using a Python environment containing Playwright (see dev/README.md).
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from custom_components.thermal_planner.analysis import replay  # noqa: E402
from custom_components.thermal_planner.analysis.demo import demo_episodes  # noqa: E402
from custom_components.thermal_planner.analysis.learning import fit_bundle  # noqa: E402
from custom_components.thermal_planner.analysis.planning import forecast_grid, make_plan  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--chrome", help="Existing Chrome/Chromium executable; otherwise use Playwright Chromium"
    )
    parser.add_argument("--output", default="/tmp/thermal-planner-browser")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    fixture = json.loads((ROOT / "tests/fixtures/cooldown.json").read_text())
    report = replay(fixture["config"], fixture["history"], fixture["start"], fixture["end"])
    report["id"] = "fixture-report"
    config = fixture["config"]
    config["heat_source"] = {
        "name": "Lab heat pump",
        "climate": "climate.fake_house_heat_pump",
        "zones": [{"id": "main", "name": "Shared zone"}],
        "pause_limit_minutes": 60,
        "dwell_minutes": 30,
        "recovery_minutes": 120,
    }
    for room in config["rooms"]:
        room["zone"] = "main"
    episodes = demo_episodes(config)
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    models = fit_bundle(config, episodes, fitted_at=now.isoformat())
    current = {
        "rooms": {room["name"]: 22 for room in config["rooms"]},
        "outdoor": 8,
        "mode": "heat",
        "heat": 1,
        "flow_target": None,
    }
    forecast = forecast_grid(
        [{"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 8} for i in range(25)],
        "°C",
        now,
        8,
    )
    plan = make_plan(config, models, current, forecast, now, "Europe/Warsaw")
    ops = {
        "readiness": {
            "can_pause": True,
            "can_observe": True,
            "can_commission": True,
            "checks": [
                {"title": "Heat pump", "ready": True, "message": "Ready to pause and restore."},
                {
                    "title": "Pause and restore check",
                    "ready": True,
                    "message": "Verified for these source settings.",
                },
            ],
        },
        "source": config["heat_source"],
        "rooms": config["rooms"],
        "mode": "advisory",
        "models": models,
        "plan": plan,
        "forecast": forecast,
        "current": current,
        "observation": None,
        "campaign": None,
        "pending": None,
        "events": [],
        "campaign_settings": {"nights": 2, "start_time": "02:00", "end_date": "2026-03-01"},
        "runs": [{k: v for k, v in episodes[0].items() if k not in ("points", "configuration")}],
    }
    serialized = json.dumps(report).replace("<", chr(92) + "u003c")
    op_serialized = json.dumps(ops).replace("<", chr(92) + "u003c")
    run_serialized = json.dumps(episodes[0]).replace("<", chr(92) + "u003c")
    # All transport is substituted; the custom element itself is the production asset.
    html = (
        """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{margin:0;font-family:system-ui;background:#f3f6f5}</style></head><body>
    <script type="module">
    import '/panel.js';
    window.mock = {report: REPORT, operations: OPS_DATA, run: RUN_DATA, calls: [], lab: {available:true,configured:true,simulated:true,status:'idle',journal:null,last_run:null}};
    const panel = document.createElement('thermal-planner-panel');
    panel.hass = {config:{time_zone:'Europe/Warsaw'},user:{is_admin:true},states:{
      'climate.fake_house_heat_pump':{state:'heat',attributes:{friendly_name:'Shared heat pump',hvac_modes:['heat','off'],temperature:23}},
      'sensor.outside_temp':{state:'8',attributes:{friendly_name:'Outside Temp',unit_of_measurement:'°C'}},
      'sensor.operating_mode_state':{state:'DHW',attributes:{friendly_name:'Operating Mode State'}},
      'sensor.main_outlet_temp':{state:'37',attributes:{friendly_name:'Main Outlet Temp',unit_of_measurement:'°C'}}},callWS:async(msg)=>{
      window.mock.calls.push(msg);
      const report = window.mock.report;
      if(msg.type==='thermal_planner/reports') return {entries:[{entry_id:'lab-entry',title:'Thermal Planner',status:'complete',room_count:4,
        reports:[{id:report.id,start:report.start,end:report.end,room_count:4,limiting_room:'Bathroom',breaches:['Bathroom'],complete:true}]}]};
      if(msg.type==='thermal_planner/report') return structuredClone(report);
      if(msg.type==='thermal_planner/analyse') {
        if(msg.start>=msg.end) throw new Error('Start must be before end.');
        await new Promise(resolve=>setTimeout(resolve,100));
        report.id='new-report'; report.outdoor_assumption_c=msg.assumed_outdoor_c ?? null; return structuredClone(report);
      }
      if(msg.type==='thermal_planner/export') return 'name,value\\nStudy,22\\n';
      if(msg.type==='thermal_planner/operations') {
        const ops=window.mock.operations;
        if(msg.action==='setup') {Object.assign(ops.source,msg.setup);}
        if(msg.action==='run_detail') return structuredClone(window.mock.run);
        if(msg.action==='run' || msg.action==='trial') window.mock.lab={...window.mock.lab,status:'paused',last_run:null,journal:{
          deadline:new Date(Date.now()+msg.minutes*60000).toISOString(),reason:null,
          events:[{at:new Date().toISOString(),message:'Climate OFF confirmed.'}]}};
        if(msg.action==='observe') ops.baseline={started:new Date().toISOString(),deadline:new Date(Date.now()+7200000).toISOString()};
        if(msg.action==='stop') {ops.baseline=null;window.mock.lab={...window.mock.lab,status:'idle',journal:null,last_run:{reason:'Stopped by user.',outcome:'restored',events:[{at:new Date().toISOString(),message:'Original Heat mode confirmed.'}]}};ops.mode='advisory';ops.campaign=ops.campaign?{...ops.campaign,active:false}:null;}
        if(msg.action==='campaign') ops.campaign={active:true,completed:0,settings:ops.campaign_settings,next_date:'2026-02-02',skips:[]};
        if(msg.action==='replan') ops.plan.id='refreshed-plan';
        if(msg.action==='apply_once') ops.mode='once';
        if(msg.action==='auto') ops.mode='auto';
        ops.control=window.mock.lab;
        return structuredClone(ops);
      }
      throw new Error('Unexpected message');
    }};
    document.body.append(panel);
    </script></body></html>""".replace("REPORT", serialized)
        .replace("OPS_DATA", op_serialized)
        .replace("RUN_DATA", run_serialized)
    )

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/":
                payload = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                super().do_GET()

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(Handler, directory=str(ROOT / "custom_components/thermal_planner/frontend")),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=args.chrome, headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1080})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.get_by_role("tab", name="Reports", exact=True)).to_have_attribute(
                "aria-selected", "true"
            )
            expect(page.get_by_role("tabpanel")).to_have_count(1)
            expect(page.locator("#lab")).to_be_hidden()
            expect(page.locator("#models")).to_be_hidden()
            expect(page.locator("#planner")).to_be_hidden()
            expect(page.get_by_role("heading", name="Temperature timeline")).to_be_visible()
            expect(page.locator("#metrics tbody tr")).to_have_count(6)
            expect(page.locator("#report svg path")).to_have_count(4)
            page.locator("#window").select_option("1")
            expect(page.locator("#metrics")).to_contain_text("Study")
            page.screenshot(path=str(output / "reports-desktop.png"), full_page=True)
            page.locator("#start").fill("2026-01-01T06:00")
            page.locator("#end").fill("2026-01-01T05:00")
            page.get_by_role("button", name="Analyse temperatures", exact=True).click()
            expect(page.get_by_role("alert")).to_contain_text("Start must be before end")
            page.get_by_text("History and outdoor temperature", exact=True).click()
            page.locator("#outdoor-mode").select_option("assumed")
            page.locator("#assumed-outdoor").fill("5")
            page.locator("#start").fill("2026-01-01T01:00")
            page.get_by_role("button", name="Analyse temperatures", exact=True).click()
            expect(page.get_by_role("status")).to_contain_text("Report saved")
            expect(page.locator("#report")).to_contain_text("Assumed outdoor temperature: 5.0°C")
            assert page.evaluate(
                "window.mock.calls.some(m=>m.type==='thermal_planner/analyse' && m.start==='2026-01-01T01:00' && m.assumed_outdoor_c===5 && m.history_policy==='until_changed')"
            )
            with page.expect_download() as download:
                page.get_by_role("button", name="Export CSV", exact=True).click()
            assert download.value.suggested_filename.endswith("new-report.csv")
            page.get_by_role("tab", name="Measurements", exact=True).click()
            expect(page.locator("#page-reports")).to_be_hidden()
            expect(page.get_by_role("tabpanel")).to_have_count(1)
            expect(page).to_have_url(f"http://127.0.0.1:{server.server_port}/#measurements")
            page.get_by_role("button", name="Controlled cooldown", exact=True).click()
            page.locator("#minutes").fill("3")
            page.get_by_role("button", name="Run once", exact=True).click()
            expect(page.locator("#lab")).to_contain_text("Climate paused")
            expect(page.get_by_role("button", name="Run once", exact=True)).to_be_disabled()
            page.get_by_role("button", name="Stop / retry restore", exact=True).click()
            expect(page.locator("#lab")).to_contain_text("Original Heat mode confirmed")
            expect(page.locator("#source")).to_contain_text("Shared zone")
            expect(page.locator("#source")).to_contain_text("Underfloor heating")
            page.get_by_role("button", name="Watch normal heating", exact=True).click()
            expect(page.locator("#lab")).to_be_hidden()
            page.get_by_role("button", name="Observe normal operation", exact=True).click()
            expect(page.locator("#observation-progress")).to_contain_text(
                "Watching normal heating until"
            )
            page.get_by_role("button", name="Stop observation", exact=True).click()
            page.get_by_role("button", name="Controlled cooldown", exact=True).click()
            page.get_by_role("button", name="Start campaign", exact=True).click()
            expect(page.locator("#lab")).to_contain_text("Campaign · active")
            page.get_by_role("button", name="Stop / retry restore", exact=True).click()
            page.screenshot(path=str(output / "measurements-desktop.png"), full_page=True)
            page.locator("#minutes").fill("3")
            page.get_by_role("tab", name="Learning", exact=True).click()
            page.get_by_role("tab", name="Measurements", exact=True).click()
            expect(page.locator("#minutes")).to_have_value("3")
            page.get_by_role("tab", name="Learning", exact=True).click()
            page.get_by_role("button", name="Fit room models", exact=True).click()
            expect(page.locator("#models")).to_contain_text("Validated")
            page.screenshot(path=str(output / "learning-desktop.png"), full_page=True)
            page.get_by_role("tab", name="Heating plan", exact=True).click()
            page.get_by_role("button", name="Refresh forecast and plan", exact=True).click()
            expect(page.locator("#planner")).to_contain_text("Supported candidate found")
            page.locator("#prediction-view").select_option("baseline")
            page.get_by_role("button", name="Apply once", exact=True).click()
            expect(page.locator("#planner")).to_contain_text("Apply once queued")
            page.get_by_role("button", name="Enable Auto", exact=True).click()
            expect(page.locator("#planner")).to_contain_text("Auto enabled")
            page.get_by_role("button", name="Stop automatic planning", exact=True).click()
            page.screenshot(path=str(output / "heating-plan-desktop.png"), full_page=True)
            page.get_by_role("tab", name="Measurements", exact=True).click()
            page.get_by_role("button", name="View timeline", exact=True).first.click()
            expect(page.locator("#run-detail")).to_contain_text("Synthetic fixture")
            page.get_by_role("button", name="Close timeline", exact=True).click()
            page.evaluate(
                "window.mock.operations.readiness={can_pause:false,can_observe:true,can_commission:false,checks:[{title:'Heat pump',ready:false,message:'Choose the Home Assistant heating control.',action:'setup'}]};window.mock.operations.source.climate=null;window.mock.lab.available=false;window.mock.lab.configured=false"
            )
            page.evaluate("document.querySelector('thermal-planner-panel').refresh()")
            expect(page.get_by_role("button", name="Run once", exact=True)).to_be_disabled()
            expect(page.locator("#measurement-checks")).to_contain_text(
                "Choose the Home Assistant heating control"
            )
            page.get_by_role("button", name="Choose heating control", exact=True).click()
            page.locator("#setup-climate").select_option("climate.fake_house_heat_pump")
            page.get_by_text("Help me match HeishaMon readings", exact=True).click()
            snapshot = {
                "heatpump": [
                    {"Name": "Outside_Temp", "Value": 8, "Description": "°C"},
                    {"Name": "Operating_Mode_State", "Value": 3, "Description": "DHW"},
                    {"Name": "Zones_State", "Value": 0, "Description": "Zone 1 active"},
                    {"Name": "Z2_Temp", "Value": -128, "Description": "°C"},
                ]
            }
            page.locator("#heishamon-file").set_input_files(
                {
                    "name": "snapshot.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(snapshot).encode(),
                }
            )
            expect(page.locator("#heishamon-preview")).to_contain_text("Zone 1 active")
            expect(page.locator("#heishamon-preview")).to_contain_text("sentinel values")
            page.locator('[data-suggestion="outdoor"]').click()
            expect(page.locator("#setup-outdoor")).to_have_value("sensor.outside_temp")
            page.get_by_role("button", name="Save selected readings", exact=True).click()
            expect(page.locator("#measurement-setup")).to_be_hidden()
            assert page.evaluate(
                "window.mock.calls.some(m=>m.action==='setup' && m.setup.outdoor==='sensor.outside_temp')"
            )
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate(
                "document.querySelector('thermal-planner-panel').narrow=true; window.addEventListener('hass-toggle-menu',()=>window.menuOpened=true)"
            )
            page.get_by_role("button", name="Open Home Assistant menu", exact=True).click()
            assert page.evaluate("window.menuOpened")
            for name in ("Reports", "Measurements", "Learning", "Heating plan"):
                page.get_by_role("tab", name=name, exact=True).click()
                expect(page.get_by_role("tabpanel")).to_have_count(1)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.get_by_role("tab", name="Reports", exact=True).click()
            page.get_by_role("tab", name="Reports", exact=True).press("ArrowRight")
            expect(page.get_by_role("tab", name="Measurements", exact=True)).to_be_focused()
            expect(page.get_by_role("tab", name="Measurements", exact=True)).to_have_attribute(
                "aria-selected", "true"
            )
            page.get_by_role("tab", name="Measurements", exact=True).press("End")
            expect(page.get_by_role("tab", name="Heating plan", exact=True)).to_be_focused()
            page.get_by_role("tab", name="Heating plan", exact=True).press("Home")
            expect(page.get_by_role("tab", name="Reports", exact=True)).to_be_focused()
            page.go_back()
            expect(page.get_by_role("tab", name="Heating plan", exact=True)).to_have_attribute(
                "aria-selected", "true"
            )
            page.go_forward()
            expect(page.get_by_role("tab", name="Reports", exact=True)).to_have_attribute(
                "aria-selected", "true"
            )
            page.reload()
            expect(page.get_by_role("tab", name="Reports", exact=True)).to_have_attribute(
                "aria-selected", "true"
            )
            page.screenshot(path=str(output / "reports-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            # Stored labels must remain text rather than becoming executable markup.
            page.evaluate(
                "window.mock.report.configuration.rooms[0].name='<img src=x onerror=window.injected=true>'"
            )
            page.locator("[data-report]").first.click()
            expect(page.locator("#report")).to_contain_text("<img src=x")
            assert page.evaluate(
                "!window.injected && !document.querySelector('thermal-planner-panel').shadowRoot.querySelector('img')"
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/#learning")
            expect(page.get_by_role("tab", name="Learning", exact=True)).to_have_attribute(
                "aria-selected", "true"
            )
            expect(page.locator("#models")).to_be_visible()
            expect(page.locator("#report")).to_be_hidden()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(f"Browser checks passed; desktop and mobile screenshots: {output}")


if __name__ == "__main__":
    main()
