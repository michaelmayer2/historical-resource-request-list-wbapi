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

## Server usage via sar (`sar.py`, enabled by `--servers`)

- One `ssh -o BatchMode=yes host sh -s` per host (host key checking disabled: `StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`, `LogLevel=ERROR`) (parallel, `server_slots()` in `run.py`), feeding the `REMOTE` script on stdin: prints `NPROC <n>` then `LC_ALL=C sadf -d <file> -- -u -r -S` for each day file. Failures warn on stderr and drop the host.
- `sadf -d` is parsed by column name: one `# hostname;interval;timestamp;...` header per activity, UTC timestamps; rows are merged per timestamp, keeping only the all-CPU row (`CPU` = `-1`).
- Day files are `saDD` (reused monthly) or `saYYYYMMDD`, named by the server's local date, so `day_files()` asks for window days ±1 in both forms and samples are then filtered to the window by timestamp (drops stale files and duplicates).
- `slot_series()` floors samples to `--sar-interval` (default 10 min) so hosts line up, averages a host's samples within a slot, then sums hosts. Hourly `_avg` / `_peak` are the mean / max over slots.
- Running scripts with `python -I` breaks `import sar` (script dir is not on `sys.path`); run normally.

## Plotting (`plot.py`)

- Reads run.py's CSV (file or `-`) and writes one self-contained HTML file: Python embeds the rows as JSON, inline JS draws SVG. No plotting dependency is installed in `.venv`; keep it dependency-free.
- Panels are defined in Python (`REQUESTED_PANELS`, plus `SAR_PANELS` when all `srv_*` columns exist); the JS is generic over them. Ratios are computed in Python; `None` (no sar data via `srv_reporting` = 0, or nothing requested) becomes a gap in the line.
- `--total-cpus` / `--total-mem-gb`: `relative()` runs after `load()` (so ratios stay absolute), adds `<field>_cap` percentage fields, and swaps them into the CPU / memory panels, keeping the originals in `abs_fields` for the tooltip. A panel's `ref` sets the label of its 100% line.
- Colors follow the dataviz skill's reference palette (avg = slot 1 blue, peak = slot 2 orange, validated light + dark); CPU and memory are separate panels (never a dual axis). Check layout by screenshotting with headless Chrome (`/Applications/Google Chrome.app`).
