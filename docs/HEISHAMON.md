# Match HeishaMon readings to Thermal Planner

You do not need to understand MQTT topics. Use the entities your existing Home Assistant integration already provides. The same reading may have a friendlier name in HA.

Open **Measurements → Set up measurement → Help me match HeishaMon readings**. Its field guide is always available. You can also save the HeishaMon `/json` page as a file and open it in the guide. Suggestions use names only; review the actual HA value before saving. Nothing is applied to your pump by importing or saving the reference.

| HeishaMon field | Meaning | Where it helps |
| --- | --- | --- |
| `Outside_Temp` | Outdoor air temperature | Outdoor sensor |
| `Main_Outlet_Temp` | Actual water leaving the pump | Optional leaving-water reading |
| `Main_Inlet_Temp` | Water returning to the pump | Optional zone return temperature |
| `Main_Target_Temp` | Requested water temperature | Read-only requested-flow feedback; it is not itself a writable HA control |
| `Operating_Mode_State` | Heating, cooling or domestic hot-water mode | Optional mode reading. Confirm which **HA values** mean DHW. |
| `Defrosting_State` | Defrost active/inactive | Optional defrost indicator |
| `Zones_State`, `Z1_Active`, `Z2_Active` | Which zones are active at that moment | A reference when reviewing your existing zones; does not prove how many are installed |
| `Z1_Sensor_Settings`, `Z2_Sensor_Settings` | What the zone sensor represents | Check before treating a zone temperature as room air |
| `Z1_Pump_State`, `Z2_Pump_State` | Secondary pump indication | Optional zone context, not proof of delivered heat |
| `Heat_Power_Consumption` | Reported electrical power | Optional energy channel; confirm units and whether it covers space heat or hot water too |
| `Heat_Power_Production` | Reported thermal output | Optional thermal channel; confirm scope and reliability |

Use the dedicated HA **climate** control that offers Heat and Off for controlled measurements. The JSON status page does not establish which HA climate entity safely controls the shared heating. Open the chosen control in HA and verify its behavior with someone present.

## Common mix-ups

- **Pump_Flow** is a volume rate (for example litres/minute). In Thermal Planner, “flow temperature” means the temperature of the heating water, not its flow rate.
- **Heatpump_State = On** means the unit is enabled, not that it is delivering heat to your rooms. DHW means domestic hot water. A cooldown should wait while the pump is heating the tank or defrosting.
- A system can expose Zone 2 fields while only Zone 1 is used. Keep one zone if that matches your installation; radiators and UFH in different rooms do not require separate pump zones.
- If zone sensor settings say **Water Temperature**, that zone reading is not a room-air sensor. Select your actual room sensors for the living room, bedrooms, etc.
- Values such as **−78°C** or **−128°C** in optional channels are likely unused/error readings, not usable room temperatures. Check the channel before connecting it.
- A snapshot describes the moment it was taken. It is not historical weather and should not silently supply the outdoor temperature for a past report. Enter a historical assumption explicitly on Reports if needed.

No direct HeishaMon polling or MQTT integration is added. The reference remains in the browser for the setup session; only the HA entities you explicitly save are stored.
