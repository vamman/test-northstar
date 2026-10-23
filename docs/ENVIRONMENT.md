# Environmental simulator integration

The independent worker reads `GET /api/impacts/transit` from `ENVIRONMENT_API_URL` before taking the database writer lock. This is a synthetic authored model, not a weather, health or economic forecast. Configure `ENVIRONMENT_API_URL` as an empty string to disable it. Bridged workers use `http://host.docker.internal:8865`; host-network workers use `http://127.0.0.1:8865`.

The vendored `environment_client.py` validates contract version, synthetic marker, peer, freshness and finite factors, caches reads for 10 seconds and fails neutral (all factors 1) when unavailable or invalid. The environment clock remains independent of this simulator's clock. Only new work changes: stored baselines are never multiplied repeatedly. Paused simulation state retains its last actual application; a read of status never advances or applies anything.

`GET /api/environment` and read-only MCP `get_environment_impact` expose the persisted last **applied** revision, source and wall observation times, `applied_simulated_at`, `applied_multipliers`, and an effect description. The six source multipliers are retained for audit; only listed applied multipliers affect this model. Manual advances without a supplied environment observation use neutral factors. Existing backlogs, elapsed work, trips, balances and clocks survive restart and recovery.

No dispatch or sibling mutation is performed by this adapter. Government sources inform the environmental simulator; these receiving rates are explicitly authored assumptions.

The status endpoint evaluates current liveness without changing saved engine evidence. `status` becomes `paused`, `worker_stale`, or `stale` for paused clocks, missing/stale worker heartbeats, or expired source validity. `applied` is true only for a currently connected, fresh, running application; `last_applied`, `observation_status`, and `application_status` retain the historical distinction.
