# Shared detailed basemap

The Transit map now draws the same published base as Police. `GET /api/basemap` serves a local, immutable copy of `northstar-basemap.json`; `GET /api/basemap/metadata` exposes its version and SHA-256, while `GET /api/basemap/features` provides bounded research pagination. Read-only MCP tools `get_basemap_metadata` and `list_basemap_features` expose the same contract.

Release `2026.09.29.1` combines the existing shared atlas with Transit-authored roads, buildings, venues and inlet. It contains 1,147 road links, 1,760 buildings and every regional facility/neighbourhood anchor. Coordinates remain metres in `northstar-local-m-v1`. The full-region map includes both rural districts; the initial city-core view retains its existing focus and scale.

The renderer loads the shared file separately from `/api/city`. Published IDs and geometry take precedence for the base. Transit routes, people, vehicles, incidents, traffic, sensors and live registered workplace buildings/access links remain domain overlays. Added regional landmarks can be inspected and located, but are not silently added to Transit incident controls or operational travel destinations.

Twenty-five building district labels are corrected to match footprint centres without moving any building. The original generator cluster is retained as `authored_cluster_id`. The shared river is drawn as a LineString with an explicitly authored visual width, alongside the inlet polygon.

This update does not alter the operational catalog or route graph. Transit still moves along its existing 1,147-edge network; `/api/spatial/route` still provides research estimates on the 93-road regional graph. The shared map metadata states this distinction. Existing run clocks, speed, people, routes, histories, IDs and frozen emergency envelopes are unchanged.

The publication source and instructions live in `/home/vince/Documents/ChatGPT/Simulators/BASEMAP.md`. `scripts/publish_basemap.py` in that repository checks deterministic generation and identical copies; publishing changed bytes requires a new version. Services serve the file locally without calls to each other. Dynamic workplace registrations are not folded into this immutable release.
