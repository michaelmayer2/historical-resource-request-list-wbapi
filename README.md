# resource-usage-wbapi

Reports hourly concurrent Posit Workbench sessions and their requested CPU / memory for a time window, as CSV. It uses only the Workbench API (no direct access to Slurm or other schedulers) and can run from any machine that reaches the Workbench server.

## 1. Set up the Python environment

Requires Python 3.13+ and `git`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "posit-sdk @ git+https://github.com/michaelmayer2/posit-sdk-py@workbench"
```

The `workbench` branch of this fork is required: it provides the API-key client (`posit.workbench.admin`) that the released SDK lacks.

## 2. Create an API token

On the Workbench server, the API must be enabled for admin tokens in `/etc/rstudio/rserver.conf` (restart `rstudio-server` after changing it):

```
workbench-api-enabled=1
workbench-api-admin-enabled=1
```

Then create a read-only **admin** token owned by a **Workbench administrator** (as root, on the Workbench node):

```bash
sudo rstudio-server generate-api-token admin 'resource-usage report' <admin-username> --read-only
```

Record the printed token; Workbench only stores a hash of it.

**The token owner must be a Workbench admin user.** An admin token owned by a non-admin user does not work. The owner username is documented as optional for admin tokens, but on Workbench 2026.09.0 a token without an owner cannot read session history (`get_historical_session` fails with "expecting a valid username"); this is fixed in later Workbench versions.

## 3. Run the report

Provide the server URL and token via environment variables:

```bash
export WORKBENCH_SERVER="https://workbench.example.com"
export WORKBENCH_API_KEY="<token>"

python run.py 2026-09-01 2026-10-01                        # all users
python run.py 2026-09-01T08:00Z 2026-09-01T18:00Z --user alice --user bob
python run.py 2026-09-01 2026-10-01 > usage.csv
```

Times are ISO-8601 (or epoch seconds); bare dates are UTC. `--server` and `--api-key` override the environment variables.

## Output columns

One row per UTC hour.

| Column | Meaning |
|---|---|
| `hour_utc` | Start of the hour |
| `running_at_hour` | Sessions running exactly at the top of the hour |
| `overlapping_in_hour` | Sessions running at any point during the hour |
| `cpu_at_hour`, `mem_mb_at_hour` | Total requested CPUs / memory (MB) of the sessions running at the top of the hour |
| `cpu_avg`, `mem_mb_avg` | Time-weighted average requested CPUs / memory over the hour (4 CPUs for 30 minutes counts as 2) |
| `cpu_peak`, `mem_mb_peak` | Highest total requested CPUs / memory at any moment in the hour |
| `unknown_limits_in_hour` | Sessions in the hour whose resource request isn't known (see below) |

## Limitations

- **Requested, not consumed.** Workbench records what each session asked for; actual CPU / memory usage is not available through its API.
- **Running Positron sessions have unknown limits.** Workbench only reports a running Positron session's request once the session has ended. While it runs it counts toward the session totals but adds 0 CPU / memory, and `unknown_limits_in_hour` is non-zero. Rerun the window after those sessions end for complete figures.
- **Start times include queue time.** Workbench usually reports only the submission time, so time spent waiting in the scheduler queue is counted as running.
- **Hours after the current time** are printed as zeros.
- A stuck ("ghost") session that Workbench still considers running is counted until it is ended, e.g. by force-quitting it from the user's Workbench homepage.
