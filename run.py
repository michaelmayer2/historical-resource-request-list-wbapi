#!/usr/bin/env python3
"""Hourly concurrent Workbench sessions between START and END.
Usage: python run.py [--server URL] [--api-key KEY] 2026-09-01T00:00:00Z 2026-09-08T00:00:00Z

Server and API key default to the WORKBENCH_SERVER / WORKBENCH_API_KEY environment
variables. Prefer the env var for the key so it doesn't end up in shell history.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from posit.workbench.admin import Client, WorkbenchError


def parse_ts(v):
    """Accept ISO-8601 strings or epoch (s/ms); return aware datetime or None."""
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)) or str(v).replace(".", "").isdigit():
        f = float(v)
        return datetime.fromtimestamp(f / 1000 if f > 1e11 else f, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None  # e.g. boost's "not-a-date-time" for unset timestamps
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # bare dates are UTC


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def requested(limits):
    """Sum CPU / memory from a resource-limit list (values are strings)."""
    out = {}
    for rl in limits or []:
        try:
            out[rl.get("type")] = float(rl.get("value") or 0)
        except ValueError:
            pass
    return out.get("cpuCount", 0.0), out.get("memory", 0.0)


# Compared lowercased: the launcher reports "Finished"/"Killed", audit records "finished"/"failed".
ENDED = {"finished", "failed", "killed", "canceled"}


def job_interval(job, now):
    """(start, end) for a launcher job, or None if it never started.

    Launcher jobs carry submissionTime / lastUpdateTime; for an ended job the last
    update is its end. Prefer explicit running/terminated times when present.
    """
    status = (job.get("status") or "").lower()
    if status == "pending":
        return None
    s = parse_ts(job.get("runningTimestamp")) or parse_ts(job.get("submissionTime"))
    if s is None:
        return None
    e = parse_ts(job.get("terminatedTime"))
    if e is None:
        e = parse_ts(job.get("lastUpdateTime")) if status in ENDED else now
    return s, e or now


def main(start, end, server=None, api_key=None, users=None):
    start, end = parse_ts(start), parse_ts(end)
    now = datetime.now(timezone.utc)
    client = Client(url=server, api_key=api_key)

    # get_session only returns the named user's sessions (and an unbound admin
    # key has no user of its own), so active sessions are queried user by user.
    if not users:
        users = [u["username"] for u in client.users.list() if u.get("username")]

    jobs, limits = {}, {}

    # 1. Ended sessions from the audit database. Each session carries its job
    #    nested as sessions[].job, with the requested resources in the session's
    #    launch_parameters. Workbench 2026.09.0 rejects this for an admin key not
    #    bound to a user ("expecting a valid username"); a user-bound admin key works.
    try:
        hist = client.call(
            "get_historical_session",
            filter_time_begin=iso(start),
            filter_time_end=iso(now),
            include_jobs=True,
            include_all_users=True,
        ) or {}
    except WorkbenchError as e:
        print(f"warning: get_historical_session unavailable ({e}); "
              "only jobs still known to the launcher are counted", file=sys.stderr)
        hist = {}
    for sess in hist.get("sessions") or []:
        j = sess.get("job")
        if j and j.get("id"):
            rl = (sess.get("launch_parameters") or {}).get("resource_limits")
            jobs[j["id"]] = {**j, "resourceLimits": j.get("resourceLimits") or rl}

    # 2. Active sessions plus ended jobs the launcher still remembers.
    #    include_all_jobs is required: include_jobs returns no jobs here.
    #    Jobs link to their session (and its resource limits) via a tag.
    for user in users:
        res = client.sessions.get(user=user, include_all_jobs=True) or {}
        for sess in res.get("sessions") or []:
            lp = sess.get("launch_parameters") or {}
            if sess.get("id") and lp.get("resource_limits"):
                limits[sess["id"]] = lp["resource_limits"]
        for j in res.get("jobs") or []:
            # Live status wins, but keep the audit record's running/terminated
            # times, which launcher jobs don't carry.
            live = {k: v for k, v in j.items() if v not in (None, "", [])}
            jobs[j.get("id")] = {**jobs.get(j.get("id"), {}), **live}

    intervals = []
    for j in jobs.values():
        se = job_interval(j, now)
        if se is None:
            continue  # never started
        s, e = se
        rl = j.get("resourceLimits") or next(
            (limits[t] for t in j.get("tags") or [] if t in limits), None)
        if e >= start and s <= end:
            # Running Positron sessions report no limits until their audit record
            # exists (i.e. until they end), so track whether the request is known.
            intervals.append((s, e, *requested(rl), bool(rl)))

    print("hour_utc,running_at_hour,overlapping_in_hour,cpu_at_hour,mem_mb_at_hour,"
          "cpu_avg,cpu_peak,mem_mb_avg,mem_mb_peak,unknown_limits_in_hour")
    t = start.replace(minute=0, second=0, microsecond=0)
    while t <= end:
        t_next = t + timedelta(hours=1)
        at = [i for i in intervals if i[0] <= t < i[1]]
        hour = [i for i in intervals if i[0] < t_next and i[1] > t]

        # Time-weighted average: each request counts for its share of the hour.
        share = [(min(i[1], t_next) - max(i[0], t)) / timedelta(hours=1) for i in hour]
        cpu_avg = sum(f * i[2] for f, i in zip(share, hour))
        mem_avg = sum(f * i[3] for f, i in zip(share, hour))

        # Peak: the total only rises when a session starts, so checking the top
        # of the hour plus each start inside it finds the maximum.
        points = [t] + [i[0] for i in hour if i[0] > t]
        cpu_peak = mem_peak = 0.0
        for p in points:
            live = [i for i in hour if i[0] <= p < i[1]]
            cpu_peak = max(cpu_peak, sum(i[2] for i in live))
            mem_peak = max(mem_peak, sum(i[3] for i in live))

        print(f"{t:%Y-%m-%dT%H:00Z},{len(at)},{len(hour)},"
              f"{sum(i[2] for i in at):g},{sum(i[3] for i in at):g},"
              f"{cpu_avg:.2f},{cpu_peak:g},{mem_avg:.0f},{mem_peak:g},"
              f"{sum(1 for i in hour if not i[4])}")
        t = t_next


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Hourly concurrent Workbench sessions.")
    ap.add_argument("start", help="window start, ISO-8601 or epoch")
    ap.add_argument("end", help="window end, ISO-8601 or epoch")
    ap.add_argument("--server", help="Workbench URL (default: $WORKBENCH_SERVER)")
    ap.add_argument("--api-key", help="Workbench API key (default: $WORKBENCH_API_KEY)")
    ap.add_argument("--user", action="append", dest="users",
                    help="only query this user (repeatable; default: all users)")
    args = ap.parse_args()
    main(args.start, args.end, server=args.server, api_key=args.api_key, users=args.users)
