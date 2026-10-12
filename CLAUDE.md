# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single script, `run.py`, that reports hourly concurrent Posit Workbench sessions (count, CPU, memory) for a time window as CSV on stdout. It talks to the Workbench launcher API from outside Workbench using an admin API key.

## Setup and running

The SDK is **not** declared in `pyproject.toml`. It must be the `workbench` branch of the fork, because `main` and upstream have no API-key client (`posit.workbench.admin`):

```
uv pip install --python .venv/bin/python "posit-sdk @ git+https://github.com/michaelmayer2/posit-sdk-py@workbench"
```

`env.sh` defines `WORKBENCH_SERVER` and `WORKBENCH_API_KEY` but does not `export` them, so source it with auto-export:

```
set -a; . ./env.sh; set +a
.venv/bin/python run.py 2026-10-12T06:00Z 2026-10-12T18:00Z          # all users
.venv/bin/python run.py 2026-09-01 2026-10-08 --user posit01          # one user
```

Bare dates are treated as UTC. `--server` / `--api-key` override the env vars. There are no tests or lint config; `py_compile` plus a live run against the server is the check.

## Workbench API behavior (verified against the live server, 2026.09.0)

These are non-obvious and drive the structure of `run.py`:

- **The admin API key has no user identity.** Any session call without an explicit user returns HTTP 401 with an empty body. `get_session(user="<name>")` works; `user="*"` does not. Hence `run.py` lists users via `client.users.list()` (`get_users`) and queries each one.
- **`get_historical_session` needs an admin key owned by a Workbench admin user on 2026.09.0.** Such a key (now in `env.sh`) works; an admin key owned by a non-admin user does not. With an unbound admin key, `include_all_users=True` fails with RPC error 100 / inner error 22 "expecting a valid username" (a bug in 2026.09.0, fixed on rstudio-pro `main` in `d27b276a1d`); without `include_all_users` it returns 401. There is no user parameter. `run.py` falls back with a stderr warning, limiting history to jobs the launcher still remembers (roughly the current day).
- **Historical results nest the job per session** (`sessions[].job`, no top-level `jobs`), with requested resources in that session's `launch_parameters.resource_limits`, and do carry `runningTimestamp` / `terminatedTime` (`not-a-date-time` when unset). When a job appears in both results, `run.py` keeps the audit timestamps and lets the live status win. The response shape is defined in `src/cpp/server/workbench-api/GetHistoricalSessionApi.cpp` in `~/projects/rstudio-products/rstudio-pro`.
- **No consumption metrics exist anywhere in the Workbench API** (or the audit DB / Prometheus metrics); only requested resources.
- **`get_session` needs `include_all_jobs=True`**; `include_jobs=True` returns zero jobs.
- **Job records have no `runningTimestamp` / `terminatedTime`.** Intervals use `submissionTime` (includes Slurm queue time) to `lastUpdateTime` for ended statuses (`Finished`, `Failed`, `Killed`, `Canceled`), or now for running jobs.
- **Job `resourceLimits` is empty.** CPU/memory come from the owning session's `launch_parameters.resource_limits`; for active sessions a job links to its session because the session's short `id` appears in the job's `tags`. Active Positron sessions report no limits (Positron doesn't write the session metadata `launch_parameters` comes from), and `get_historical_session` only returns ended sessions, so a running Positron session's request is unknown until it ends. The `unknown_limits_in_hour` column counts these; rerunning a window after its sessions end fills them in.
- **Status casing differs:** the launcher reports `Running`/`Finished`/`Killed`, audit records `finished`/`failed`; compare lowercased. In practice audit records have `terminatedTime` but an empty `runningTimestamp`, so starts fall back to `submissionTime`.
- RPC errors from the SDK only expose code and message; to see the inner error, POST to `api/<method>` via `client.post(...)` and print the raw body.
