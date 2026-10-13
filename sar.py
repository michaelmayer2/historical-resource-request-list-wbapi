"""Measured server usage from sysstat (sar) history, fetched over ssh.

Each server is queried with `sadf -d`, sysstat's machine-readable form of the data `sar`
shows: semicolon-separated, UTC timestamps, one `# hostname;interval;timestamp;...` header
per activity (-u CPU, -r memory, -S swap).
"""
import shlex
import subprocess
from datetime import datetime, timedelta, timezone

# Output metric -> how to compute it from one merged sadf sample, given the host's CPU count.
# CPU percentages become CPU-equivalents; kB become MB.
METRICS = {
    "srv_cpu_user": lambda s, n: s["%user"] / 100 * n,
    "srv_cpu_nice": lambda s, n: s["%nice"] / 100 * n,
    "srv_cpu_system": lambda s, n: s["%system"] / 100 * n,
    "srv_cpu_iowait": lambda s, n: s["%iowait"] / 100 * n,
    "srv_cpu_steal": lambda s, n: s["%steal"] / 100 * n,
    "srv_cpu_idle": lambda s, n: s["%idle"] / 100 * n,
    "srv_mem_used_mb": lambda s, n: s["kbmemused"] / 1024,
    "srv_mem_cache_mb": lambda s, n: (s["kbbuffers"] + s["kbcached"]) / 1024,
    "srv_swap_used_mb": lambda s, n: s["kbswpused"] / 1024,
}
FIELDS = {"%user", "%nice", "%system", "%iowait", "%steal", "%idle",
          "kbmemused", "kbbuffers", "kbcached", "kbswpused"}

REMOTE = """\
d={sa_dir}
if [ -z "$d" ]; then
  for c in /var/log/sa /var/log/sysstat; do [ -d "$c" ] && d=$c && break; done
fi
echo "NPROC $(nproc)"
for f in {files}; do
  [ -r "$d/$f" ] && LC_ALL=C sadf -d "$d/$f" -- -u -r -S
done
true
"""


def day_files(start, end):
    """sa file names covering [start, end] in any server time zone (window days +-1).

    sysstat names files saDD (day of month, reused monthly) or, with long names,
    saYYYYMMDD. Stale saDD files from an earlier month are dropped later by timestamp.
    """
    names, d = [], (start - timedelta(days=1)).date()
    while d <= (end + timedelta(days=1)).date():
        names += [f"sa{d:%Y%m%d}", f"sa{d:%d}"]
        d += timedelta(days=1)
    return list(dict.fromkeys(names))


def parse_ts(v):
    """sadf -d timestamps look like '2026-10-12 10:10:00 UTC' (older versions omit UTC)."""
    try:
        dt = datetime.fromisoformat(v.removesuffix(" UTC").strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_sadf(text):
    """Return (ncpu, {timestamp: {field: value}}) from the remote script's output."""
    ncpu, header, samples = None, None, {}
    for line in text.splitlines():
        if line.startswith("NPROC "):
            ncpu = int(line.split()[1])
            continue
        if line.startswith("# "):
            header = line[2:].split(";")
            continue
        row = line.split(";")
        if not header or len(row) != len(header):
            continue  # e.g. LINUX-RESTART markers
        rec = dict(zip(header, row))
        if rec.get("CPU", "-1") not in ("-1", "all"):
            continue  # per-CPU rows; keep the all-CPU total
        ts = parse_ts(rec.get("timestamp", ""))
        if ts is None:
            continue
        s = samples.setdefault(ts, {})  # merges -u/-r/-S rows; dedupes repeated files
        for k in FIELDS & rec.keys():
            try:
                s[k] = float(rec[k].replace(",", "."))
            except ValueError:
                pass
    return ncpu, samples


def fetch(host, start, end, ssh_user=None, sa_dir=None):
    """ssh to host and return (ncpu, samples) within [start, end). Raises on failure."""
    target = f"{ssh_user}@{host}" if ssh_user else host
    script = REMOTE.format(sa_dir=shlex.quote(sa_dir or ""), files=" ".join(day_files(start, end)))
    # Host keys are not checked or recorded: nodes are often recreated (new keys,
    # reused names / instance IDs) and are typically reached via an SSM proxy.
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR",  # silence the "Permanently added ..." warning
         target, "sh", "-s"],
        input=script, capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"ssh exited with {r.returncode}")
    ncpu, samples = parse_sadf(r.stdout)
    if not ncpu:
        raise RuntimeError("could not determine CPU count (nproc)")
    samples = {ts: s for ts, s in samples.items() if start <= ts < end and FIELDS <= s.keys()}
    if not samples:
        raise RuntimeError("no sysstat samples in the window (is sysstat collecting?)")
    return ncpu, samples


def slot_series(per_host, interval):
    """Sum hosts per time slot: {slot: {"hosts": n, "cpus": n, metric: value}}.

    Samples are floored to `interval` so different hosts line up; a host's samples within
    one slot are averaged first, so a finer sar interval doesn't count a host twice.
    """
    step = interval.total_seconds()
    slots = {}
    for ncpu, samples in per_host.values():
        mine = {}
        for ts, s in samples.items():
            slot = datetime.fromtimestamp(ts.timestamp() // step * step, tz=timezone.utc)
            mine.setdefault(slot, []).append({m: f(s, ncpu) for m, f in METRICS.items()})
        for slot, vals in mine.items():
            agg = slots.setdefault(slot, {"hosts": 0, "cpus": 0, **dict.fromkeys(METRICS, 0.0)})
            agg["hosts"] += 1
            agg["cpus"] += ncpu
            for m in METRICS:
                agg[m] += sum(v[m] for v in vals) / len(vals)
    return slots


def hour_columns(slots, t, t_next):
    """CSV values for one hour: cpus, reporting hosts, then avg/peak per metric."""
    hour = [v for k, v in slots.items() if t <= k < t_next]
    if not hour:
        return ["0", "0"] + ["0"] * (2 * len(METRICS))
    out = [f"{max(v['cpus'] for v in hour):g}", f"{min(v['hosts'] for v in hour)}"]
    for m in METRICS:
        vals = [v[m] for v in hour]
        out += [f"{sum(vals) / len(vals):.2f}", f"{max(vals):.2f}"]
    return out


def header():
    return ["srv_cpus", "srv_reporting"] + [f"{m}_{a}" for m in METRICS for a in ("avg", "peak")]
