# Northstar commuting and modeled road traffic

Northstar now has an authored daily travel cycle: weekday trips leave residential
neighbourhoods for workplaces and school anchors in the morning, reverse toward
homes in the afternoon/evening, and shift toward social travel on weekends.
Airport, cruise, rail-arrival and event demand continue independently.

This is a fictional demand model. It is not calibrated to a real city, school
board, traffic counter or 200,000-person population survey.

## Three distinct representations

| Representation | Meaning | Counting rule |
| --- | --- | --- |
| Traveler circle | One actual synthetic individual in the persisted travel sample | Existing 5,000-person cap, stable `p…` identity, real queue/boarding/arrival lifecycle |
| Registered office itinerary | A person supplied by a connected office registry with linked daily itinerary | Part of the individual sample, not an additional population total |
| Road vehicle mark | One moving cohort representing **five vehicles** assigned to the same origin/destination path | Separate aggregate layer, at most 1,200 visible cohorts; must not be added to traveler totals |

Road occupancy estimates use 1.35 people per private car, 1.3 per taxi and 20 per
school bus. Each road mark therefore represents five cars/taxis/buses and an
estimated passenger load. These are transparent coefficients, not passengers
allocated to individual dot identities. Some travel markets overlap. Adding
the road passenger estimate to individual traveler counts would double count.

Scheduled public buses remain the existing finite transit vehicles with actual
sample passengers. Road-layer bus marks represent additional school-service
demand; they do not claim to be extra copies of those scheduled buses.

## Daily schedules

All schedule calculations use `America/Moncton`, including daylight-saving
changes. Times below are local. Smooth bell-shaped departure profiles avoid
instantaneous changes at hour boundaries.

| Purpose | Centre of peak | Width | Authored peak vehicle departures/hour |
| --- | --- | --- | --- |
| Work outbound | 07:48 | 1.25 hours | 12,500 |
| Work midday shift | 12:30 | 1 hour | 1,000 |
| Work homebound | 17:00 | 1.45 hours | 13,500 |
| School outbound | 08:03 | 0.65 hours | 4,800 |
| School homebound | 15:06 | 0.9 hours | 5,600 |

Weekend work demand is 20% of these work profiles, representing shift work.
Background/social vehicle demand combines 850 departures/hour, a 14:00 broad
peak (7,800 on weekdays or 10,000 on weekends, width four hours), and a 20:00
evening peak (2,600, width 1.8 hours). These figures describe the separate
aggregate road layer, not the number of new traveler dots.

School demand is enabled Monday–Friday, excluding July/August and December
20–January 4. The schedule deliberately does not claim real statutory holidays,
in-service days or a real school board's calendar. School demand anchors use
existing community/university map locations and are labeled in the API; they
are not new surveyed facilities or changes to the shared Northstar atlas.

The individual traveler sample releases purpose-tagged work/school journeys at
10% of the numerical vehicle departure profile. This is an independent authored
sampling setting, **not a population expansion factor**. Unstructured background
sample demand remains 0.36 people/second during the day, 0.14 overnight, with a
0.9 weekend multiplier. Gateway and event releases retain their own accounting.

Sample journeys carry `trip_purpose`, `home_id`, `activity_place_id` and
`commute_direction`. Morning routes run home→activity and afternoon routes
activity→home over fixed authored origin/destination pairs. These background
journeys are independent trip samples, not a claim that every morning circle
reappears as the same resident in the evening. Connected office registries have
their own linked outbound/return itineraries.

When a transit route has suitable stops near both ends, sample commuters walk
to their stop, enter the real queue, board a finite vehicle, then walk the last
leg if needed. One individual identity survives those legs. Otherwise they use
the existing direct road-path modes. Waiting time starts when the access walk
ends and the traveler enters the transit queue.

## Physical road movement and congestion

Road cohorts take shortest-distance paths on the catalog's actual connected
road graph. Persisted distance along the path determines position and heading.
They cannot move through buildings or take straight-line shortcuts off roads.
Street speeds are applied separately as each segment is crossed, including
when a single simulation step crosses several segments.

The model assigns current origin/destination departure flow to each road on the
selected paths. Roads expose `flow_vehicles_hour`, `capacity_vehicles_hour`,
`congestion_pct` and `speed_kph`. Flows are assigned demand estimates; they are
not counter observations. Both directions share the following authored capacity:

| Road class | Two-way modeled capacity/hour |
| --- | --- |
| Arterial | 4,800 vehicles |
| Local street | 1,600 vehicles |
| Access road | 1,200 vehicles |

Speed fraction is `max(0.08, 1 / (1 + 0.15 × (flow/capacity)^4))`. This produces
different congestion by road segment and time of day. It is an authored
volume-delay response, not a validated traffic assignment model. School buses
use 85% of that segment speed. Scheduled road buses and direct road travelers
also respond to their current segment's congestion.

An active severe-weather incident reduces road capacity to 72%. A collision
within 600 metres reduces local segment capacity to 35%, and a signal fault
reduces it to 60%. These factors and the incident's other existing transport
effects are explicit simulation assumptions. Infrastructure power availability
also affects transit service through the separate infrastructure model.

Traffic, air-quality and noise sensor proxies use the weighted road vehicles
within 650 metres once the road layer is installed. Footfall and queue counters
continue to count individual sample people. The two traffic representations are
not added together. Sensor readings remain authored responses, not real meters.
The weighted traffic layer uses a separate air-quality response coefficient of
0.035 per nearby represented vehicle, and noise adds
`6 × log10(1 + nearby represented vehicles / 10)`. Reusing the old sparse taxi
sample's linear coefficients would inflate alerts simply because the modeled
traffic population increased. These remain synthetic signal proxies, not
emissions, dispersion or acoustic predictions.

Road assignments refresh each simulated minute. Road positions advance on the
same bounded steps as the rest of the engine. Startup and upgrades initialize
a stated 15-minute in-flight demand window, capped at 1,000 cohorts. Subsequent
departures consume a fractional accumulator. If the 1,200-cohort display/model
limit is reached, suppressed vehicle journeys are counted, not silently hidden.

Vehicle accounting conserves:

```text
generated vehicle journeys = represented vehicles still moving + completed vehicle journeys
requested vehicle journeys = generated vehicle journeys + suppressed vehicle journeys
```

Road cohorts do not independently create patients or emergency calls. The
existing individual-traveler exposure model, registered office road exposure,
manual scenarios and upstream 911 simulator retain their incident ownership.
This avoids multiplying emergency demand simply by the vehicle display weight.

## API fields and persistence

Dashboard snapshots include `road_traffic` with `vehicles`, `roads`, `summary`,
24 hourly `profile` points, at most 180 minute `history` points and explicit
assumptions. `summary.work_trips` and `summary.school_trips` count individual
sample journeys generated since this model was installed. Fields containing
`vehicle_journeys` are weighted road-layer counts. `represented_people` is an
occupancy estimate, not a registry count or full regional population.

`ensure_road_traffic(state)` adds missing state without changing existing
traveler IDs, incidents, office registries, clocks or outbound request bodies.
Its random sequence is separate from the original traveler engine. Positions,
accumulators and counters survive JSON/database restart. Read-only snapshots
never initialize or advance a missing historical road layer.

## Conceptual references

FHWA describes broad work and school travel time-of-day patterns in
[Our Nation's Highways, Chapter 4](https://www.fhwa.dot.gov/policyinformation/pubs/hf/pl11028/chapter4.cfm)
and discusses morning/evening traffic peaks in
[Our Nation's Highways 2026: Travel](https://www.fhwa.dot.gov/policyinformation/pubs/our_nations_highways_2026/travel.cfm).
These sources motivate the use of distinct morning, afternoon-school and evening
peaks. They do **not** supply or validate Northstar's rates, road capacities,
occupancy factors, school calendar or synthetic geography.
