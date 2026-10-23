# Northstar facility utilities

This module models **authored facility utility operations**, driven by the actual traveler and transit-vehicle sample. It does not represent measured buildings, a calibrated electric grid, an engineering design, a utility bill, or a hidden full-city population. All capacities, coefficients, reserves and failure responses below are scenario assumptions. No network, database or wall-clock calls occur in the model.

The conceptual separation of lighting, heating/cooling, hot-water and equipment uses is consistent with the categories considered by the [U.S. Department of Energy Building Energy Asset Score](https://www.energy.gov/cmei/buildings/building-energy-asset-score). That reference is not the source of Northstar's numeric assumptions and is not a claim of certification or predictive accuracy.

## Facilities, activities and clocks

There are **12 facility anchors**: every M1 subway stop, the airport, rail interchange, harbour, North Commons depot, South Bank interchange, East River Pier and Cove Marina, with duplicates removed. Their IDs and metre coordinates come directly from the shared city catalog. There are **34 explicit elevators** across those facilities.

Each nearby modeled person is assigned to at most one facility within 450 metres. Riders in moving transit vehicles are excluded; riders in vehicles stopped for boarding can contribute to local terminal activity. Changes in membership generate entry/exit counts. Vehicles within 350 metres add equipment activity. These are activity proxies for this bounded simulation sample, not occupancy counts for all real-world building users.

`ensure_infrastructure(state)` is an additive, idempotent migration. It preserves the simulation clock, all passengers, existing utility meters and outages. The first installation records the **current** simulation time, starts cumulative meters at zero, and establishes person/vehicle baselines without recording fictional prior consumption. Battery reserves and water tanks are explicit initial inventories, separately labeled. Missing intervals are recorded as `unobserved_seconds`; they never receive retrospective meter estimates. The current schema has a fast validation path so ordinary steps perform one activity scan, not repeated migration scans.

The engine calls `advance_infrastructure(state, dt)` **after transport movement but before incrementing `state['t']`**. The accounted interval is `[state['t'], state['t'] + dt]`. The function leaves the main clock unchanged and sets infrastructure `observed_at` to the interval end. Repeating an already-accounted interval raises an error. Internal utility integration steps are at most 10 seconds and split at outage expiry boundaries. Operator changes are immediate outage records; facility readings retain their displayed observation time until the next model step.

`infrastructure_snapshot(state)` is strictly read-only, including absent or archived infrastructure. It does not install meters, expire outages or advance history. Returned nested objects can be modified by a caller without modifying the saved state.

## Electricity and traction

| Facility profile | Base load kW | Grid capacity kW | Backup output kW | Initial backup kWh | Elevators |
| --- | ---: | ---: | ---: | ---: | ---: |
| Airport | 480 | 1,800 | 260 | 520 | 8 |
| Harbour | 200 | 800 | 110 | 220 | 4 |
| Rail interchange | 150 | 1,000 | 90 | 180 | 4 |
| Depot | 100 | 500 | 70 | 140 | 2 |
| Station/interchange | 38 | 650 | 25 | 50 | 2 |
| Ferry terminal | 28 | 140 | 20 | 40 | 2 |

Authored baseline shares are HVAC 36%, lighting 24%, equipment 25%, and hot water 15%. HVAC and lighting have a 0.68 multiplier between 01:00 and 05:00 local time. Each present modeled person adds 0.040 kW HVAC and 0.018 kW hot-water load. Each nearby transit vehicle adds 3 kW of terminal equipment load. Elevators contribute explicit motor/idle loads. These are operating proxies rather than a thermodynamic building model.

Subway and regional-rail **traction demand** is derived from actual distance traversed along the vehicle's route during the step. The authored coefficients are 2.4 kWh/km per subway train and 4.2 kWh/km per regional train, multiplied by `0.75 + 0.25 × occupancy/capacity`. Demand is allocated equally among the modeled facility feeders on that route. Station/interchange grid capacities include this authored traction allowance. `end_uses_kw.traction` and the separate traction meters distinguish this from building loads. The snapshot's `systems` rows report subway and rail requested kW, served kW and consumed kWh. There is no claim to simulate acceleration, gradients, regenerative braking, substations, voltage or feeder topology. Aircraft, marine propulsion, bus fuel and private-vehicle fuel are outside this electricity ledger.

Grid power serves the load up to available capacity. A power outage reduces that capacity by its severity. Backup storage supplies remaining demand up to both its output limit and available energy. Spare grid capacity recharges storage up to 35% of the backup output limit. This v1 uses ideal charging/discharging efficiency; losses are not silently hidden in the ledger.

Priority circuits comprise 28% of baseline load plus elevator motor and idle demand. Their served fraction governs elevator motion, communications and pumps. Remaining served power is assigned to traction up to its request, and the rest covers other building uses. A route's next-step operating factor reflects the most constrained station's electric service. Subway/rail can stop after complete loss of grid and reserve; surface routes retain a 35% degraded operating floor. This coupling uses the previous utility interval, so it has at most a normal 10-second integration lag. Requested traction is based on realized movement and is not a validated electric traction solver.

Power readings distinguish instantaneous `demand_kw`, `served_kw`, `grid_kw`, `backup_kw`, `charging_kw`, and `unserved_kw`. `grid_kw` includes charging. Cumulative energy is in kWh. Their conservation identities are:

```text
demand_kwh = consumed_kwh + unserved_kwh
consumed_kwh = grid_kwh - charged_kwh + backup_kwh
backup_remaining_kwh = backup_initial_kwh + charged_kwh - backup_kwh
kWh = kW × seconds / 3600
```

`traction_kwh` is a subset of consumed power, not another quantity to add to facility totals. The elevator energy meters are also subsets of facility electricity. End-use kW values describe requested load; service curtailment is visible separately.

## Elevators

Every elevator has an ID, a 12-person car, queue, in-car count, remaining trip duration, status, completed trips/passengers and consumed energy. A trip takes an authored 30 seconds, with a 9 kW motor and 0.08 kW idle draw. Partial critical-power availability slows a trip. Metered motion can end at a step boundary, giving the same small discretization used in other facility operations.

Sixteen percent of observed entry/exit transitions create vertical-circulation service requests, using a persisted fractional carry so requests remain integers and deterministic. Waiting requests form a shared facility pool and are reassigned to available lifts. People already in an out-of-service car remain in that car until service restores. Fractional elevator failures disable `ceil(severity × number of cars)` explicit assets. Recovery resumes interrupted trips; it does not fabricate completed passengers or erase the queue.

```text
requests_people = completed_passengers + queue_people + in_car_people
```

These service requests are an activity-derived utility sample, not a second population of city dots. Individual requests are not linked to individual city-person IDs, and elevator queues do not independently gate those people's transit boarding. The explicit asset queue is useful for utility load and accessibility-stress scenarios; detailed indoor navigation remains outside this v1.

## Internet

Facility demand is `8 + 0.65 × present_people + 1.8 × nearby_vehicles + 0.25 × new_elevator_requests` Mbps. Nominal capacities are 2,500 Mbps at the airport, 1,000 Mbps at harbour/rail, 500 Mbps at the depot, 300 Mbps at stations and 250 Mbps at ferry terminals. `capacity_mbps` is nominal; `available_capacity_mbps` includes outage severity and available critical power.

Served throughput cannot exceed available capacity. A simple utilization response increases latency as capacity fills. `latency_ms` is **null when offline**, because a completed network round trip cannot be measured. The response is an authored proxy rather than a packet-level network model.

```text
GB = Mbps × seconds / 8000       # decimal GB, eight bits per byte
demand_gb = transferred_gb + unserved_gb
```

## Water and sewer

| Facility profile | Supply m³/h | Tank m³ | Baseline demand m³/h | Sewer pumping m³/h | Sewer buffer m³ |
| --- | ---: | ---: | ---: | ---: | ---: |
| Airport | 75 | 100 | 12 | 65 | 18 |
| Harbour | 28 | 36 | 5 | 24 | 8 |
| Rail interchange | 20 | 24 | 3 | 18 | 6 |
| Depot | 12 | 18 | 2 | 10 | 4 |
| Station/interchange | 6 | 8 | 0.7 | 5 | 2 |
| Ferry terminal | 5 | 7 | 0.5 | 4 | 2 |

Tanks begin at 65% capacity, recorded as `initial_tank_m3`. Consumption adds 0.0015 m³ per present person-hour and 0.0018 m³ per newly observed entry to the profile's baseline. Available inlet capacity reflects the water outage and electric pump availability. Reserves can maintain consumption while supply is interrupted; `unserved_m3` increases only when demand exceeds available inlet plus stored volume. Refill is bounded by tank capacity.

Sewer inflow comprises 92% of water consumed plus an authored 0.03 m³/h background infiltration. Pumping is limited by available sewage capacity and critical electricity. Excess accumulates in the finite buffer; only the volume exceeding that buffer becomes `overflow_m3`. Restoring service drains backlog at the modeled pumping capacity. This is a conservative lumped-volume model, not a pipe-pressure, treatment chemistry or flood-routing model.

```text
initial_tank_m3 + supplied_m3 - consumed_m3 = tank_m3
demand_m3 = consumed_m3 + unserved_m3
sewer.inflow_m3 = treated_m3 + backlog_m3 + overflow_m3
m³ = m³/h × seconds / 3600
```

Rate fields describe the latest interval; cumulative meters describe only time actually observed since installation. Capacities and available capacities are both exposed, so an outage is distinguishable from low demand.

## Disruptions and interfaces

`create_disruption(state, facility_id, utility, duration_minutes, severity)` accepts a known facility, one of `power`, `elevators`, `internet`, `water`, or `sewer`, a finite duration from 1 to 1,440 minutes and a finite severity from 0.01 to 1.0. Unknown facilities and restore IDs raise `LookupError`; invalid values or a conflicting active outage for the same utility/facility raise `ValueError`. Outage identifiers are unique within the simulation run. Outages may coexist across utilities or facilities. They expire at their authored time or can be restored manually; restoring an already-ended outage is idempotent.

The module does not send emergency messages or call other simulators. The application owns APIs, authentication, persistence, MCP and any deliberate escalation policy. These outages are synthetic operations scenarios, not commands to real equipment.

Snapshot shape:

```text
model_version, installed_at, observed_at, scope, synthetic, unobserved_seconds
summary: instantaneous utility totals and cumulative energy/data/volume totals
facilities[]: id, name, x, y, kind, activity, power, elevators, internet, water, sewer, status
systems[]: id, name, demand_kw, served_kw, consumed_kwh
disruptions[]: id, facility_id, utility, severity, started_at, ends_at, status, restored_at?
history[]: time, power_demand_kw, power_served_kw, energy_kwh,
           elevator_queue_people, internet_mbps, water_m3_h, sewer_backlog_m3
```

All public quantities identify their units in their keys. History retains the latest 360 samples, at most one per simulated minute. Statuses are `normal`, `warning` or `critical`; explicit elevator asset states are `idle`, `moving`, `paused` or `out_of_service`.

The focused tests verify additive/idempotent migration, read-only archived snapshots, lack of historical backfill, unit conversions, all conservation identities, request-driven elevator trips, available-car assignment, interrupted-car preservation, backup depletion/recharge, Internet failure, tank depletion, sewage overflow/recovery, exact outage expiry, validation, restart determinism, duplicate-interval protection, traction-distance energy and split-interval traction accounting. Run `python -m pytest tests/test_infrastructure.py` in the project environment.
