# resource-usage-wbapi

Reports hourly concurrent Posit Workbench sessions and their requested CPU / memory for a time window, as CSV. It uses only the Workbench API (no direct access to Slurm or other schedulers) and can run from any machine that reaches the Workbench server.

## 1. Set up the Python environment

Requires Python 3.13+ and `git`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt 
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

### Optional: measured usage from the servers (sar)

Add `--servers` with a comma-separated host list to also report what the servers actually used, read from their sysstat (`sar`) history over ssh:

```bash
python run.py 2026-10-01 2026-10-08 --servers node1,node2,node3 [--ssh-user admin]
```

Requirements on each server:

- `sysstat` installed and collecting (`systemctl enable --now sysstat`). Data is read from `/var/log/sa` (RHEL) or `/var/log/sysstat` (Debian/Ubuntu); override with `--sa-dir`.
- Non-interactive ssh from the machine running the script (keys or agent; `~/.ssh/config` applies). The script never prompts for passwords. Host keys are not checked or saved to `known_hosts` (`StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`), so recreated nodes don't break the run. This also means the server's identity isn't verified, so use it only on networks you trust or through an authenticated proxy such as AWS SSM.
- History only goes back as far as sysstat keeps it (`HISTORY` in `/etc/sysconfig/sysstat` or `/etc/sysstat/sysstat`, typically 7–28 days).

An unreachable server, or one without data, is reported on stderr and left out; `srv_reporting` shows how many servers contributed.

## 4. Plot the report

`plot.py` turns the CSV into a self-contained HTML page (no extra packages): stacked panels of hourly average and peak on a shared time axis, with a hover readout and a data table.

- Always: requested CPUs and requested memory (GB).
- If the CSV was made with `--servers`: measured CPU (`srv_cpu_user_*`, in CPUs), measured memory used (`srv_mem_used_mb_*`, in GB), and measured ÷ requested in % for CPU and memory (avg ÷ avg, peak ÷ peak; 100% = using exactly what was requested). Hours where no server reported, or nothing was requested, are gaps.

Read the ratios with care. sar measures the **whole server**, including the OS and anything outside Workbench, while requests only cover Workbench sessions. So the ratio runs high when little is requested (e.g. the tail hour of a session). It is only meaningful if `--servers` lists every node the sessions ran on.

```bash
python plot.py usage.csv -o usage.html
python run.py 2026-09-01 2026-10-01 | python plot.py - -o usage.html
python plot.py usage.csv -o usage.html --total-cpus 64 --total-mem-gb 256
```

Open `usage.html` in a browser. It follows the browser's light / dark setting.

`--total-cpus` and `--total-mem-gb` (each optional, independent) give the system's total capacity. The requested and measured CPU / memory panels are then shown as a percentage of it, with a "= total capacity" line at 100%. The tooltip keeps the absolute value next to the percentage. The measured ÷ requested ratio panels are not affected.

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

With `--servers`, these columns follow. Values are summed across servers; each `_avg` is the mean of the hour's sar samples and each `_peak` the highest one.

| Column | Meaning |
|---|---|
| `srv_cpus` | Total logical CPUs of the servers that reported |
| `srv_reporting` | Fewest servers that reported in any sample of the hour (0 = no data) |
| `srv_cpu_user`, `_nice`, `_system`, `_iowait`, `_steal`, `_idle` (`_avg` / `_peak`) | sar's `%user` etc. converted to CPU-equivalents: `% / 100 × logical CPUs`. 25% user on an 8-CPU server is 2.00 |
| `srv_mem_used_mb_avg` / `_peak` | sysstat's `kbmemused`, in MB |
| `srv_mem_cache_mb_avg` / `_peak` | Buffers + page cache (`kbbuffers + kbcached`), in MB |
| `srv_swap_used_mb_avg` / `_peak` | Swap in use (`kbswpused`), in MB |

## Limitations

- **Requested, not consumed.** Workbench records what each session asked for; actual CPU / memory usage is not available through its API. Use `--servers` for measured usage.
- **sar figures are 10-minute averages.** sysstat samples every 10 minutes by default, so `_peak` is the busiest 10-minute interval, not an instantaneous spike. Samples from different servers are aligned to 10-minute slots (`--sar-interval` to change).
- **`kbmemused` is sysstat's definition** and has changed between sysstat versions (whether it excludes buffers/cache). It is not the same as summed process RSS.
- **Running Positron sessions have unknown limits.** Workbench only reports a running Positron session's request once the session has ended. While it runs it counts toward the session totals but adds 0 CPU / memory, and `unknown_limits_in_hour` is non-zero. Rerun the window after those sessions end for complete figures.
- **Start times include queue time.** Workbench usually reports only the submission time, so time spent waiting in the scheduler queue is counted as running.
- **Hours after the current time** are printed as zeros.
- A stuck ("ghost") session that Workbench still considers running is counted until it is ended, e.g. by force-quitting it from the user's Workbench homepage.
