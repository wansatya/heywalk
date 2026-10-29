# HeyWalk API backend

FastAPI service per `AGENTS.md` sections 5 and 10–12: project/file management,
controller generation and fix requests (via the Meta Muse Spark service
interface), run orchestration with a validated state machine, live WebSocket
telemetry, objective evaluation records, and project export.

## Run

```bash
cd services/api
uv venv .venv  # first time only
uv pip install --python .venv/bin/python -e '.[test]'
cp ../../.env.example ../../.env  # set MODEL_API_KEY server-side
HEYWALK_DB=./heywalk.db .venv/bin/uvicorn app.main:app --port 8000
```

The frontend (`apps/web`) proxies same-origin `/api` calls to port 8000 in dev.

## Notes

- SQLite storage (single-user local MVP, AGENTS.md section 11). Schema is
  created automatically; `HEYWALK_DB` selects the file.
- Generated controller code is **never executed in this process**. Runs are
  driven by `app/services/execution.py` through the isolated-runner interface
  in `app/runner/`. `RUNNER_BACKEND` selects the backend: `none` (default;
  honest not-configured error), `docker` (production: one single-use
  non-root ROS 2 Jazzy container per run, no network, CPU/memory/process
  limits, wall-clock timeout, killed and removed on cancel/timeout), or
  `local` (dev/test subprocess fallback running the same harness over the
  loopback topic transport -- NOT container isolation).
- The controller talks to the simulator only over ROS 2 topics (`/cmd_vel`,
  `/odom`, `/scan`, `/emergency_stop`, `/goal_pose`; see
  `app/runner/topics.py`). The harness (`app/runner/harness.py`) runs inside
  the boundary, collects `telemetry.jsonl`, and writes `result.json`; the
  objective evaluator owns the success verdict. The ROS 2 package skeleton
  lives in `ros2_ws/src/heywalk_controller`; runner/API images and the
  local stack are defined in `docker/` and `docker-compose.yml` (build the
  runner image with `docker compose --profile runner-build build runner`).
  In compose the API orchestrator holds the Docker socket so it can launch
  sibling run containers; the socket is never mounted into a run container,
  and generated code never runs in the API process.
- Corrections (`POST .../fix`) are capped at `MAX_FIX_ATTEMPTS` (default 2)
  per failed run, restricted to existing files plus `controller.py`, and
  rolled back on write failure; reruns (`POST /api/runs/{id}/rerun`) start
  a fresh simulation with the same world config and seed.
- `POST .../generate` and `POST .../fix` call the live Muse Spark client
  in `app/services/meta_model.py` (Chat Completions at `META_API_BASE_URL`,
  model `MUSE_MODEL`, JSON-schema structured output, 3 bounded retries).
  Without a configured `MODEL_API_KEY` they return an explicit 503
  integration error — never a fabricated result. Verify connectivity first
  with `MODEL_API_KEY=[redacted] python scripts/smoke_model.py`.
- `DevMockMetaModelClient` is a clearly-labeled fake for frontend dev/tests
  only; `build_client` never returns it and its output must never be
  presented as a live model result.
- `WS /api/runs/{run_id}/stream` replays stored history, then live events.
  Telemetry is paced server-side to at most ~20 messages/second.

## Tests

```bash
.venv/bin/python -m pytest -q
```
