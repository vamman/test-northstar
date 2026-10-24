# Cruise ship terminal operations

The cruise terminal extension models the **port-side reality of a cruise call** for a smart-city port authority: ships berthing, passenger groups clearing customs, spreading into the city, and returning to board before departure. It is an authored, synthetic operations layer — not observed port data, a berth-booking system, or a navigation aid.

## Scope and time scale

- **One port** at the shared `northstar_harbour` anchor (26500, 22500), **one terminal**, and the scheduled cruise calls already produced by the arrivals timetable (June–September; Tuesday/Thursday/Saturday/Sunday, with a second peak-season Saturday call). Peak Saturdays are therefore **multi-ship days** out of the box.
- **Three city destinations** for the MVP: Harbour Market & Boardwalk (capacity 900), Cove Marina (400) and Bay Reach Beach (600). Capacities are authored scenario values.
- The layer follows the **shared simulation clock**. At the default speed of 60, one real second equals one simulated minute — a full 08:00–15:30 call plays out in about 7½ wall minutes.
- Passenger **groups** (1–6 people, ~12% with reduced mobility, a per-person budget and two interest tags) are the modeling unit. Groups are capped (`group_cap`, default 600); anything beyond the cap is counted as `suppressed_people` instead of being silently dropped.

## Event flow

Each call (`visit`) moves through `scheduled → (holding) → disembarking → ashore → boarding → departed`:

1. **Berthing** — a ship takes the lowest free berth. If all berths are occupied (for example under a `berths: 1` scenario), the ship holds outside the harbour and disembarkation waits.
2. **Disembarkation** — modeled passengers come down the gangway over the call's disembarkation window and join the **customs queue**. Lanes serve groups FIFO (`customs_seconds_per_group`, default 45 s per group per lane).
3. **Choice and travel** — cleared groups pick a destination (interest appeal × weather × remaining capacity headroom) and a mode: **walk** (≤ 1.8 km, never for most reduced-mobility groups), **terminal shuttle** (fixed headway, bounded capacity, groups board only if they fit whole) or **taxi** (stand dispatches one group per interval, stretched by road congestion and the environment travel-time multiplier).
4. **Visit** — groups occupy their destination against its capacity (overflow waits), dwell 25–110 minutes by venue, and spend a budget-derived amount recorded per destination.
5. **Return and boarding** — groups leave by a planned return time (with an 8% authored chance of a delayed party), clear the **security queue**, and board. At departure, anyone not aboard is counted as **missed**.

Positions interpolate along real street geometry from the shared catalog graph; queue positions sit at the terminal anchor. Leg speeds are frozen at departure as authored proxies. This layer does not board transit vehicles and does not alter the engine's passenger accounting.

## Randomness and determinism

All randomness (group sizes, mobility, budgets, interests, destination and mode choices, dwell, delays) flows through a **separate xorshift RNG stream** stored in the terminal sub-state and seeded from the run seed. The main engine's random stream is never consumed, so adding or running this layer does not change existing engine behavior or histories. A JSON restart reproduces the terminal state exactly.

Weather enters through the environment layer's `demand` multiplier (suppressing outdoor "nature" appeal) and `travel_time` multiplier (stretching taxi/shuttle legs); with no environment feed both are neutral 1.0. Road congestion stretches taxi dispatch and vehicle speeds.

## Metrics

- **Return-to-ship rate** — boarded ÷ (boarded + missed), per call and cumulative.
- **At-risk passengers** — groups not yet in the security queue whose estimated arrival (remaining travel + queue drain) is inside 15 minutes of departure. An estimate, not a guarantee.
- **Throughput** — disembarked and boarded people per hour over the recent history window.
- **Queues** — customs/security groups and people, taxi and shuttle queues, next shuttle departure.
- **Destination occupancy** — people in place vs capacity, waiting groups, cumulative visitors and spend.
- **All aboard** — minutes to boarding start and departure for the imminent call, with ashore and at-risk counts.

## Operational insights

`insights` in the payload are authored advisories derived only from the terminal's own state, ordered alert → watch → info: all-ashore/all-aboard alerts during the boarding window, holding-for-berth alerts, customs/security wait estimates (≥ 8 min watch, ≥ 15 min alert), shuttle demand exceeding twice its capacity, multi-ship day notices, and destinations at ≥ 90% occupancy. They are decision support for the simulated port authority, not predictions beyond the model.

## Scenarios

`POST /api/cruise-terminal/scenario` (admin `X-Transit-Token`) updates terminal settings, validated against fixed limits: `berths` (1–3), `customs_lanes` (1–12), `security_lanes` (1–12), `customs_seconds_per_group`, `security_seconds_per_group`, `shuttle_capacity_people` (0 disables the shuttle), `shuttle_headway_minutes`, `taxi_interval_seconds`, `shore_minutes`, `boarding_minutes`, `group_cap`. Settings apply to **calls created afterwards**; a call already berthed keeps the schedule it arrived with. Reduced berths/lanes reproduce congestion and holding scenarios; the default two-berth config serves the peak-Saturday pair without holding.

## API and dashboard

- `GET /api/cruise-terminal` — full detail: ships, queues, destinations, totals, insights, throughput, all-aboard, a bounded group sample (first 250), the terminal event log and per-minute history.
- `GET /api/dashboard` — includes the compact `cruise_terminal` summary.
- The dashboard's **Cruise terminal** tab shows the KPI strip, insight cards, ship call cards with disembarkation progress, queue rows, destination occupancy bars and the terminal log. The map's **Cruise** chip overlays berth status rings (amber pulse during all-aboard risk) and passenger-group dots colored by state: amber queues, mode-colored transit, teal visiting, red overflow.

## Boundaries

Group counts here are a **separate bounded operational layer**: do not add them to the individual traveler sample, road cohorts, or infrastructure loads. Manifest sizes remain scenario assumptions; only modeled group members are simulated. Missed-departure counts are model outcomes of the authored queue, delay and return-time assumptions — not records of real events.
