---
doc_id: SOP-HVAC-002
doc_type: sop
title: Chiller Trip Alarm - Diagnosis and Reset
tags: [chiller, trip, alarm, reset, high pressure, low pressure, flow switch, freeze, oil pressure, overload, hvac, fault]
applies_to: [chiller]
version: "3.0"
owner: MEP Supervisor
last_reviewed: 2026-04-01
---

# Chiller Trip Alarm - Diagnosis and Reset

## Scope and safety
Applies to all chillers in the central plant. Only trained HVAC technicians may reset a chiller trip. Never bypass, jumper or disable a safety device to keep a chiller running. Apply lock-out/tag-out (LOTO) before opening any electrical panel.

## Step 1 - Record the alarm before resetting
1. Note the alarm code and description exactly as shown on the chiller controller, with date and time.
2. Note leaving/entering chilled water and condenser water temperatures, suction and discharge pressures, and motor current at the time of trip (from the controller history or BMS trend).
3. Inform the MEP Supervisor and switch the standby chiller ON if cooling load requires it.

## Step 2 - Identify the trip type and check the cause
- High condenser (discharge) pressure trip: check the condenser water pump is running, cooling tower fans are ON, cooling tower strainer is not choked, and condenser water valves are open. Repeated HP trips usually mean fouled condenser tubes. The HP switch is often manual-reset.
- Low suction / low refrigerant pressure trip: check chilled water flow and temperature. A persistent low-pressure trip indicates a possible refrigerant leak - do NOT keep resetting; call the AMC vendor for a leak test.
- Chilled water or condenser water flow switch trip: check the relevant pump, strainer and valve positions; check for air in the line.
- Low leaving water temperature (freeze protection) trip: check the set-point and chilled water flow; do not lower the freeze set-point.
- Oil pressure / oil level trip: check the oil level sight glass and oil heater. Escalate to the vendor.
- Motor overload / high motor temperature / phase fault: check incoming supply for phase loss or unbalance. Escalate to the electrical technician.

## Step 3 - Reset procedure
1. Correct the cause identified in Step 2.
2. Acknowledge the alarm on the controller. For manual-reset safeties (e.g. HP switch), press the physical reset button on the switch.
3. Press RESET on the controller and wait for the anti-recycle timer to complete before restart.
4. Restart the chiller as per SOP-HVAC-001 and monitor for at least 30 minutes.

## When NOT to reset - escalate instead
- The same trip has occurred twice within 24 hours.
- The cause could not be identified or corrected.
- Any low refrigerant pressure, oil, or motor/electrical trip.
In these cases keep the chiller OFF, raise a P2 work order, and call the AMC vendor.

## Records
Every trip must be recorded as a work order with alarm code, readings, cause found, action taken and restart time.
