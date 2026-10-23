# Northstar transport model

This is an authored simulation of a fictional modern city. It produces simulated operations, **not observed city data, calibrated forecasts, census totals, or live sensor measurements**. Every passenger dot represents exactly one modeled person. Airport and cruise manifest sizes are scenario assumptions; only the separately labeled `modeled_passengers` enter the bounded sample.

The road-traffic extension uses separate weighted vehicle cohorts, with work/school schedules and local road congestion documented in [COMMUTING.md](COMMUTING.md). Facility utility loads, elevator operations, cumulative meters, resource balances and outages are documented in [INFRASTRUCTURE.md](INFRASTRUCTURE.md). Individual passenger counts, aggregate road traffic and facility engineering demand have distinct scopes and must not be added together as one population count.

## Shared geography

The vendored Northstar atlas remains authoritative for district polygons and facilities. Coordinates use `northstar-local-m-v1`, a fictional local engineering grid in **metres**, not latitude/longitude. The airport anchor is `(15500, 11500)`, rail interchange `(-11500, 9300)`, and harbour `(26500, 22500)`. Incident locations include the canonical CRS, units, district, precision, provenance, and exact shared facility ID where applicable.

The transit catalog adds 68 urban places, 1,128 visible street links, 1,760 building footprints, and six dense district street grids. All added geography is authored. Buildings are visual context, not an inventory of occupied households. A schematic inlet suggests coastal geography; it is not a surveyed shoreline or a navigation chart.

Walking, cycling, taxi and bus movement follows shortest-distance paths on an explicit, connected graph. Links include local roads, the shared regional atlas road graph, and explicit facility-access connectors. Dedicated subway/rail track and ferry-channel polylines are separate mode networks. Every moving dot is interpolated by distance along its network path; queued persons remain at their boarding place and onboard persons occupy their actual vehicle's position. Overlapping passengers aboard a vehicle are still distinct agents.

## Service and capacity

| Service | Capacity per vehicle | Nominal headway | Uncongested speed |
| --- | ---: | ---: | ---: |
| M1 Northstar subway | 420 | 6 min | 57.6 km/h |
| A1 Airport Express | 65 | 10 min | 36 km/h |
| C2 Coastal Connector | 65 | 12 min | 39.6 km/h |
| N3 University & Stadium | 65 | 8 min | 32.4 km/h |
| S4 South Bank | 65 | 10 min | 32.4 km/h |
| W5 Foundry Shuttle | 55 | 12 min | 32.4 km/h |
| R1 Regional Rail | 280 | 30 min | 72 km/h |
| F1 Harbour Waterbus | 100 | 20 min | 28.8 km/h |

The route, stop, shape, service-day, and headway concepts are informed by the official [GTFS Schedule reference](https://gtfs.org/documentation/schedule/reference/). These authored values are not an imported GTFS feed or a claim of GTFS conformance. There is exactly one subway line.

Seventy-three initial transit vehicles are spaced along their routes. Fleet counts are sized from a nominal round-trip duration and headway, with a per-route ceiling of 20 vehicles. Vehicles reverse at terminals, dwell for 20 seconds at bus/subway stops and 30 seconds at rail/ferry stops, and board waiting passengers in arrival order until capacity is exhausted. Unboarded people remain queued. There is no standing-room overflow beyond the stated capacity. Headways are targets established through initial spacing; congestion and disruption change subsequent service spacing.

The service day runs from 05:00 to 01:00 local time. No new transit trips are created outside service hours; existing vehicles pause until morning. This is a simplified overnight shutdown, not a full depot or last-service model. Walkers move at 1.35 m/s, cyclists at 4.8 m/s, and taxis at up to 11 m/s. Congestion reduces taxi/bus speed. The road extension adds private vehicle flows and per-link capacity; car ownership, lane changing, detailed traffic-signal phases, parking and full-day personal itineraries are not modeled. Event/arrival passengers can have a walk leg after alighting.

## Demand, arrivals and events

The default scenario begins **July 18, 2026 at 08:00 Atlantic Daylight Time**, with 2,400 seeded agents distributed among in-progress trips, vehicles and queues. Local civil time uses `America/Moncton`, including seasonal UTC offsets. A caller must provide an explicit offset for alternate starts.

Background trip releases vary by local time, with morning/evening peaks and a weekend modifier. Six daily flights release 125 modeled people each across 15-minute disembarkation windows; six regional train arrivals release 180 modeled people each over eight minutes. Their larger scenario manifests are shown separately.

Cruise calls occur **June through September only**, on Tuesday, Thursday, Saturday and Sunday. Peak-season Saturdays have a second call. The first vessel's scenario manifest is 2,100, with 600 modeled passengers released over 40 minutes. The second manifest is 1,650, with 420 modeled passengers released over 35 minutes. Arrivals feed real airport/harbour/rail-origin queues and taxi trips. These external arrival records are passenger demand injections. A separate `gateway_vehicles` display layer animates aircraft and cruise ships along deterministic authored paths: ten-minute flight approach/seven-minute departure and fifteen-minute ship approach/departure, with a stop at the exact shared gateway during disembarkation. These schematic silhouettes are derived from the timetable and do not represent flight/marine physics or alter passenger/capacity accounting.

The catalog contains **28 annually recurring events covering all 12 months**: festivals, sports, conferences, markets, graduation, community celebrations and seasonal port events. A date jump selects a new scenario date rather than fast-forwarding unobserved attendance. Events attract trips to their actual venue. The two-hour lead-in releases an authored sample equal to 2% of event attendance; active events add 0.2% per hour. During the hour after an event ends, a separate 2% egress trip sample originates at the venue. `modeled_attendance` counts generated arriving sample trips, and `modeled_departures` counts generated egress trips. These are distinct trip samples, not tracked returns of the same all-day population. Event demand counts never imply that thousands of hidden full-city people have been simulated.

## Bounded demand and conservation

At most **5,000 modeled persons** are active simultaneously. Demand that cannot enter at this ceiling is counted in `suppressed_demand`; it is not silently discarded or rendered as an implied dot. Arrival records also expose their own generated and suppressed modeled passenger counts. The primary conservation identities are:

```text
generated_people = active_people + completed_trips
demand_requested = generated_people + suppressed_demand
arrival.processed_people = arrival.generated_people + arrival.suppressed_people + arrival.before_start_people
```

For a scenario beginning after an arrival's disembarkation has already begun, `before_start_people` records the modeled passengers whose release time predates the run; those passengers are not replayed or counted as newly generated/suppressed demand. Incoming visitors are directed only to approaching or active events, never to events already dispersing.

Completed trip agents leave the active sample. This prevents indefinite growth while retaining cumulative throughput. A seeded person already aboard a vehicle is part of the same conservation ledger. The city's population from the shared atlas is context only and never used as a fabricated live mobility total.

## Smart sensors and disruptions

Eighty synthetic sensors measure five authored responses at 16 locations: local modeled footfall, waiting passengers, traffic pressure, particulate proxy and noise proxy. Counts use modeled people within 650 metres. The environmental values use explicit formulas driven by local activity, not independent random numbers, external environmental feeds or physical dispersion/acoustic models. Thresholds trigger normal/warning/critical states; unresolved local incidents force sensor alarms. These readings are useful for integration demonstrations, not public-health or engineering decisions.

The congestion percentage is an authored proxy derived from active taxis, waiting passengers and open incidents. It affects bus/taxi progress. Signal failure stops its selected route. Collisions, crowd surges and medical incidents slow the affected route; storms slow bus and ferry service. People moving near incidents also slow. This approximates local and network impacts without modeling traffic lanes or evacuations.

Operators may create collision, medical, crowd-surge, signal-failure and storm scenarios with 0–30 casualties. An exposure process also produces low-rate medical/minor-collision events—roughly one per three simulated hours around 2,500 active people, adjusted by congestion—with a ceiling of three open endogenous incidents. These rates and severities are authored, not epidemiological predictions.

Each incident has a run-specific globally distinct identifier. Emergency operational status is monotonic: late dispatch reports cannot regress an incident from on-scene, transporting or resolved. Hospital/clinic reports update clinical status but do not close a transport incident. An emergency's resolved/closed report must identify 911 as its source. A local signal failure with no casualties may resolve through transit maintenance after 20 simulated minutes, unless dispatch is pending, sent, retrying or blocked. Emergency incidents never expire themselves. The application layer owns durable outbox delivery, authentication, deduplication and integrations.

## Reproducibility and time

The engine persists its numeric random-generator state, all persons, vehicle progress, queues, arrival processing counters, event accumulators and incident counters as ordinary JSON. Restarting from that state and advancing with the same sequence of increments produces identical subsequent motion and demand. Run UUIDs distinguish independent runs; they are the only unseeded identity component. Catalog changes require deliberate handling of existing saved states because path geometry and route definitions are referenced by stable IDs.

`advance(state, seconds)` advances exactly the requested simulated duration using internal steps of at most ten seconds. It does not apply the display speed or pause flag itself; the worker applies those controls. A stop arrival consumes the current integration step rather than carrying fractional unused movement through multiple stops, so travel times have bounded integration discretization. The dashboard history retains the latest 360 samples, sampled no more frequently than once per simulated minute. API/UI interpolation is presentation only and does not move agents in the model.

## Verification

The engine tests check shared anchors and district membership, a full-year calendar, geometry-constrained movement, passenger/vehicle capacity conservation, restart determinism, summer/winter cruise behavior and offsets, explicit suppressed demand, event arrival/egress destinations, incident location validity, stopped/recovered routes, exposure incidents, maintenance restrictions, and out-of-order 911/hospital feedback. Run them with `python -m pytest tests/test_engine.py` in the project's Python environment.
