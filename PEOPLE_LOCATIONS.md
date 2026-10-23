# Source-owned live people locations

Every simulator exposes read-only `GET /api/people/locations` using
`northstar.people-locations.v1`. City collects those reports at `GET /api/locations`
(`northstar.city-locations.v1`). NorthStar Live consumes City, with direct source
reports as a fallback. Collection never advances a simulator or writes its state.

Reports refresh in the background (normally every eight wall seconds), retain
source simulation time and wall observation time, and expose live/stale/offline
status. A failed refresh retains the last observation and marks it stale; it does
not change that observation's timestamp. Coordinates use schematic
`northstar-local-m-v1` metres, not real GPS. Paused source clocks stay paused.

Each located record has a stable source-scoped ID, role/status, represented person
count, location basis/precision, and any known floor/room/area. Unlocated people
remain in coverage totals but are omitted from the located-record payload.
Coverage counts represent people, while `records` counts rows. Hospital inpatient
counts are anonymous department cohorts; no patient charts, home addresses, or
clinical identities are exposed. Office reads only presence fields from a single
read-only database snapshot, avoiding repeated rich roster pagination.

| Source | Current location evidence |
| --- | --- |
| Office / Factory | Explicit onsite attendance, building/plant and available floor/area |
| Clinics | Active clinician assignments to clinic/room |
| Hospital | Staff's current department; anonymous occupied inpatient departments |
| Police / Fire | Verified active unit/apparatus crew assignments and modeled unit points |
| Transit | Source-modeled traveller positions; run-scoped identities and transport mode |
| 911 | Operators currently working at the command facility |
| University | Explicit onsite staff and current class attendance; campus/building precision |
| Bank | No location evidence; financial identities are not a resident-location roster |
| City / Environment | No owned individual people roster; aggregate conditions/capacity only |

City preserves source provenance and removes duplicate Office/Transit records only
when an explicit worker ID establishes the link. Names never establish identity.
Unresolved overlap between source rosters remains possible: their summed reports
are not a unique resident census. The roughly 200,000-person City census is a
separate aggregate and does not establish current positions for all residents.
Live's viewport counts use the complete available location report, independently
of sprite rendering limits; stale reports and residential census estimates are
labeled separately. A facility location is not precise indoor tracking.

Shared implementation and isolated behavioral tests live in `northstar-shared`.
`scripts/publish_people_locations.py` vendors the reviewed publisher/adapters and
minimal atlas anchors into each simulator. The API remains read-only and uses no
new control credentials, ports, database migrations, or worker processes.
