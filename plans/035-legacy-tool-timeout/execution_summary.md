# 035 — execution summary

Version 0.57.2 applies a declared timeout to live legacy pblang `tool_call` steps. The run now records an explicit timeout/uncertain-effect error and does not automatically retry that call, even when the step declared retries. Dry-run and successful result shapes are unchanged. The package, manifest, runtime metadata and retained lockfile version are aligned; pre-existing dependency edits in `uv.lock` were preserved. `uv lock --check --offline` passes.

The original red timeout reproduction passes. A new test verifies that a timed-out tool configured with two retries runs only once. The complete plugin suite is **759 passed, 3 failed** in 70.63 seconds, improved from 757 passed / 4 failed at the 0.57.1 baseline. The three unchanged red legacy-v1 reproductions concern restart, owner-approval wait and event wait. They need a durable resume/decision/event design or safe migration; simply making a run wait forever would not fix the product.

The fixed dojoP candidate loaded Playbooks 0.57.2 beside the sixteen defaults and DB (`dojoP/results/20260916-fixed-candidate-boot-final.json`); all 44 offline contracts and 13 PostgreSQL contracts remain passing in the `final2` receipts. The model episode still depends on a valid provider credential and is not counted as a mission result.
