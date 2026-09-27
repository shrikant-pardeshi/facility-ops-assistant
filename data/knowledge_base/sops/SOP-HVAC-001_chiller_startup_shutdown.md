---
doc_id: SOP-HVAC-001
doc_type: sop
title: Chiller Start-up and Shutdown
tags: [chiller, hvac, start-up, shutdown, sequence, chilled water, condenser water, cooling tower]
applies_to: [chiller]
version: "2.1"
owner: MEP Supervisor
last_reviewed: 2026-04-01
---

# Chiller Start-up and Shutdown

## Scope
Applies to water-cooled screw chillers in the central chiller plant. To be carried out only by trained HVAC technicians or the AMC vendor. The BMS normally sequences chillers automatically; this SOP covers manual start-up and shutdown during changeover, maintenance or BMS failure.

## Pre-start checks
1. Confirm the chiller is not under lock-out/tag-out (LOTO) and no work permit is open on it.
2. Check chilled water and condenser water isolation valves for the selected chiller are OPEN.
3. Confirm cooling tower fans and condenser water pump are available; check cooling tower basin level and make-up water.
4. Verify control panel shows no active alarms and the compressor oil heater has been ON for at least 12 hours (screw compressor requirement after long shutdown).
5. Check supply voltage on the starter panel: all three phases present, unbalance under 2%.

## Start-up sequence
1. Start the cooling tower fan(s) and the condenser water pump. Wait for stable condenser water flow (flow switch made).
2. Start the chilled water primary pump. Wait for stable chilled water flow (flow switch made).
3. Enable the chiller from the local controller or BMS. The compressor starts after the internal anti-recycle timer.
4. Monitor for 30 minutes: leaving chilled water temperature (target 6.5 to 7.0 C), suction and discharge pressures, oil pressure, motor current (should not exceed rated full-load amps).
5. Record readings in the chiller log sheet.

## Shutdown sequence
1. Disable the chiller from the controller. The compressor unloads and stops.
2. Keep the chilled water and condenser water pumps running for at least 5 minutes after the compressor stops (pump-down / heat dissipation).
3. Stop the condenser water pump and cooling tower fans, then the chilled water pump (unless another chiller is running).
4. Keep the oil heater power ON unless the chiller is being isolated for maintenance.

## Records
Log every start/stop with time, reason, and readings. Any abnormal reading must be reported to the MEP Supervisor and recorded as a work order.
