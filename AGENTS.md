# Northstar transit working conventions

Use FastAPI, SQLAlchemy/PostgreSQL, vanilla Canvas dashboard and the official Python MCP SDK. SQLite is for isolated tests and development. Keep all people and operations synthetic; no real emergency integrations.

Preserve the shared atlas anchors and use metre coordinates in northstar-local-m-v1. New roads, buildings and venues are an additive transit overlay. Never rewrite sibling simulator files or their existing histories as an incidental change.

Persist exact outbound emergency envelopes transactionally with incident creation. Keep external identities and body hashes stable across retries. 911 owns multi-agency dispatch; never independently create a duplicate EMS dispatch. Preserve source clocks and wall observation time separately. Do not manufacture responder/hospital progress when a source is paused.

MCP stays read-only and receives no database, control, ingestion or upstream credentials. Keep .env, databases, backups and screenshots out of Git. Do not widen network bindings incidentally.

Run relevant engine/API/integration tests, use isolated state for destructive scenarios, and inspect UI changes in a browser. Update model and integration documentation when behavior changes.

GitHub publication is authorized to `CanXPAI/canxp-simulation-transit`, with canonical remote branch `main`. The local `origin` uses `git@github.com:CanXPAI/canxp-simulation-transit.git`. After completing and validating requested transit changes, commit only the related finished files and push to `origin/main` with a normal fast-forward push. Verify the remote commit afterwards. Preserve unfinished work from parallel tasks, local simulation data, credentials and ignored artifacts. Do not automatically stage unrelated dirty files, rewrite shared history or force-push. If remote history has diverged or publishing is blocked, preserve the local work and report the issue. The recurring sync checks completed commits; it does not turn unfinished working-tree edits into release commits.
