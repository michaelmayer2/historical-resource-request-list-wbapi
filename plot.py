#!/usr/bin/env python3
"""Plot run.py's hourly CPU / memory (avg and peak) as an HTML page.

Usage: python plot.py usage.csv [-o usage.html] [--title TEXT]
                      [--total-cpus N] [--total-mem-gb GB]
       python run.py 2026-09-01 2026-10-01 | python plot.py - -o usage.html

Writes one self-contained HTML file (inline SVG, no dependencies): stacked panels
on a shared time axis, with a hover readout and a data table. Always plotted:
requested CPUs and memory. When the CSV has run.py's --servers (sar) columns, also
measured CPU (user) and memory used, and measured / requested ratios.
--total-cpus / --total-mem-gb show the CPU / memory panels as % of that capacity.
"""
import argparse
import csv
import json
import sys

REQUIRED = ("hour_utc", "cpu_avg", "cpu_peak", "mem_mb_avg", "mem_mb_peak")
SAR = ("srv_cpu_user_avg", "srv_cpu_user_peak", "srv_mem_used_mb_avg", "srv_mem_used_mb_peak")

# id, name, unit, decimals, (avg field, peak field), 100% reference label.
# Fields are keys in each row; the title is "name (unit)".
REQUESTED_PANELS = [
    ("cpu", "Requested CPUs", "CPUs", 2, ("cpu_avg", "cpu_peak"), None),
    ("mem", "Requested memory", "GB", 1, ("mem_avg", "mem_peak"), None),
]
SAR_PANELS = [
    ("srv_cpu", "Measured CPU, user", "CPUs", 2, ("srv_cpu_avg", "srv_cpu_peak"), None),
    ("srv_mem", "Measured memory used", "GB", 1, ("srv_mem_avg", "srv_mem_peak"), None),
    ("cpu_ratio", "CPU: measured ÷ requested", "%", 0, ("cpu_ratio_avg", "cpu_ratio_peak"), "= requested"),
    ("mem_ratio", "Memory: measured ÷ requested", "%", 0, ("mem_ratio_avg", "mem_ratio_peak"), "= requested"),
]
CPU_PANELS, MEM_PANELS = {"cpu", "srv_cpu"}, {"mem", "srv_mem"}


def num(v):
    return float(v) if v not in (None, "") else None


def pct(measured, requested):
    """measured / requested in %, or None when either is unknown or nothing was requested."""
    if measured is None or not requested:
        return None
    return 100 * measured / requested


def load(f):
    reader = csv.DictReader(f)
    cols = set(reader.fieldnames or [])
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        sys.exit(f"error: CSV lacks column(s) {', '.join(missing)}; is it run.py output?")
    has_sar = all(c in cols for c in SAR)

    rows = []
    for r in reader:
        row = {
            "t": r["hour_utc"],
            "cpu_avg": num(r["cpu_avg"]) or 0.0,
            "cpu_peak": num(r["cpu_peak"]) or 0.0,
            "mem_avg": (num(r["mem_mb_avg"]) or 0.0) / 1024,   # memory is plotted in GB
            "mem_peak": (num(r["mem_mb_peak"]) or 0.0) / 1024,
            "unknown": int(r.get("unknown_limits_in_hour") or 0),
        }
        if has_sar:
            # srv_reporting = 0 means no server delivered data: a gap, not a measured 0.
            reported = (num(r.get("srv_reporting")) or 0) > 0 if "srv_reporting" in cols else True
            get = (lambda c: num(r[c])) if reported else (lambda c: None)
            mem_mb = lambda c: None if get(c) is None else get(c) / 1024
            row.update({
                "srv_cpu_avg": get("srv_cpu_user_avg"),
                "srv_cpu_peak": get("srv_cpu_user_peak"),
                "srv_mem_avg": mem_mb("srv_mem_used_mb_avg"),
                "srv_mem_peak": mem_mb("srv_mem_used_mb_peak"),
            })
            # ratios pair like with like: avg with avg, peak with peak
            row.update({
                "cpu_ratio_avg": pct(row["srv_cpu_avg"], row["cpu_avg"]),
                "cpu_ratio_peak": pct(row["srv_cpu_peak"], row["cpu_peak"]),
                "mem_ratio_avg": pct(row["srv_mem_avg"], row["mem_avg"]),
                "mem_ratio_peak": pct(row["srv_mem_peak"], row["mem_peak"]),
            })
        rows.append(row)
    if not rows:
        sys.exit("error: CSV has no data rows")
    panels = [dict(zip(("id", "name", "unit", "decimals", "fields", "ref"), p))
              for p in REQUESTED_PANELS + (SAR_PANELS if has_sar else [])]
    for p in panels:
        p["title"] = f"{p['name']} ({p['unit']})"
    return rows, panels


def relative(rows, panels, total_cpus=None, total_mem_gb=None):
    """Show CPU / memory panels as % of total capacity, keeping absolute values for the tooltip.

    Runs after load(), so the measured / requested ratios stay based on absolute values.
    """
    for p in panels:
        total = (total_cpus if p["id"] in CPU_PANELS else
                 total_mem_gb if p["id"] in MEM_PANELS else None)
        if not total:
            continue
        pct_fields = tuple(f"{f}_cap" for f in p["fields"])
        for row in rows:
            for f, pf in zip(p["fields"], pct_fields):
                row[pf] = None if row.get(f) is None else 100 * row[f] / total
        cap = f"{total:g} {p['unit']}"
        p.update(abs_fields=p["fields"], abs_unit=p["unit"], abs_decimals=p["decimals"],
                 fields=pct_fields, unit="%", decimals=1, ref="= total capacity",
                 title=f"{p['name']} (% of {cap})")


def render(rows, panels, title):
    # "</" inside a <script> block would end it early; JSON allows "<\/".
    js = lambda v: json.dumps(v, ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("__TITLE__", js(title))
                .replace("__PANELS__", js(panels))
                .replace("__DATA__", js(rows)))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workbench resources</title>
<style>
.viz-root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --border: rgba(11,11,11,0.10);
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7;
  --series-1: #2a78d6; --series-2: #eb6834;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19; --border: rgba(255,255,255,0.10);
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --grid: #2c2c2a; --baseline: #383835;
    --series-1: #3987e5; --series-2: #d95926;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --border: rgba(255,255,255,0.10);
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
  --grid: #2c2c2a; --baseline: #383835;
  --series-1: #3987e5; --series-2: #d95926;
}
html, body { margin: 0; }
.viz-root { background: var(--page); color: var(--text-primary); min-height: 100vh;
  font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 24px; box-sizing: border-box; }
h1 { font-size: 18px; font-weight: 600; margin: 0 0 4px; }
.sub { color: var(--text-secondary); margin: 0 0 16px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px 16px 8px; margin-bottom: 16px; position: relative; }
.card h2 { font-size: 14px; font-weight: 600; margin: 0; }
.legend { display: flex; gap: 16px; color: var(--text-secondary); font-size: 12px; margin: 4px 0 4px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.key { width: 16px; height: 2px; border-radius: 1px; display: inline-block; }
svg { display: block; width: 100%; overflow: visible; }
svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--text-muted); }
svg text.dl { font-weight: 600; font-size: 12px; fill: var(--text-secondary); }
svg text.empty { font-size: 13px; fill: var(--text-muted); }
.tip { position: absolute; pointer-events: none; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 6px; padding: 8px 10px; font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.12);
  white-space: nowrap; display: none; z-index: 2; }
.tip .when { color: var(--text-secondary); margin-bottom: 4px; }
.tip .row { display: flex; align-items: center; gap: 8px; }
.tip .row b { min-width: 56px; text-align: right; font-variant-numeric: tabular-nums; }
.tip .row span { color: var(--text-secondary); }
.tip .note { color: var(--text-muted); margin-top: 4px; }
details { margin-top: 8px; }
summary { cursor: pointer; color: var(--text-secondary); }
table { border-collapse: collapse; margin-top: 8px; font-variant-numeric: tabular-nums; font-size: 12px; }
th, td { padding: 3px 10px; text-align: right; border-bottom: 1px solid var(--grid); }
th { color: var(--text-secondary); font-weight: 600; }
th:first-child, td:first-child { text-align: left; }
</style>
</head>
<body>
<div class="viz-root">
  <h1 id="title"></h1>
  <p class="sub" id="sub"></p>
  <div id="panels"></div>
  <details><summary>Data table</summary><table id="table"></table></details>
</div>
<script>
const TITLE = __TITLE__;
const PANELS = __PANELS__;
const DATA = __DATA__;
const SERIES = [
  { key: "avg",  idx: 0, label: "Hourly average", short: "avg",  color: "var(--series-1)" },
  { key: "peak", idx: 1, label: "Hourly peak",    short: "peak", color: "var(--series-2)" },
];
const NS = "http://www.w3.org/2000/svg";
const W = 960, H = 200, M = { top: 12, right: 56, bottom: 26, left: 48 };
const times = DATA.map(d => new Date(d.t.replace(/Z$/, ":00Z")));
const pad2 = n => String(n).padStart(2, "0");
const fmtHour = t => `${t.getUTCFullYear()}-${pad2(t.getUTCMonth() + 1)}-${pad2(t.getUTCDate())} ${pad2(t.getUTCHours())}:00 UTC`;
const val = (d, p, s) => d[p.fields[s.idx]];
const fmtNum = (v, unit, dec) => `${v.toFixed(dec)}${unit === "%" ? "%" : " " + unit}`;
const fmtVal = (v, p) => v == null ? "no data" : fmtNum(v, p.unit, p.decimals);
// capacity-relative panels keep the absolute value alongside the percentage
const fmtAbs = (d, p, s) => p.abs_fields && d[p.abs_fields[s.idx]] != null
  ? ` (${fmtNum(d[p.abs_fields[s.idx]], p.abs_unit, p.abs_decimals)})` : "";

document.getElementById("title").textContent = TITLE;
document.getElementById("sub").textContent = `${fmtHour(times[0])} – ${fmtHour(times[times.length - 1])}` +
  (PANELS.length > 2 ? " · requested allocations (Workbench) and measured usage (sar)"
                     : " · requested allocations, not measured usage");

function el(name, attrs, parent) {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
}
const x = i => M.left + (DATA.length === 1 ? 0 : i * (W - M.left - M.right) / (DATA.length - 1));

// x ticks: midnights for multi-day ranges, every few hours otherwise
const spanH = DATA.length;
const stepH = spanH > 72 ? 24 * Math.ceil(spanH / 24 / 10) : spanH > 24 ? 6 : spanH > 12 ? 3 : 1;
const xTicks = times.map((t, i) => [t, i]).filter(([t]) => stepH >= 24
  ? t.getUTCHours() === 0 && Math.round((t - times[0]) / 864e5) % (stepH / 24) === 0
  : t.getUTCHours() % stepH === 0);
const fmtTick = t => stepH >= 24 || t.getUTCHours() === 0
  ? `${pad2(t.getUTCMonth() + 1)}-${pad2(t.getUTCDate())}` : `${pad2(t.getUTCHours())}:00`;

const host = document.getElementById("panels");
const panels = PANELS.map(p => {
  const card = document.createElement("div"); card.className = "card";
  const h2 = document.createElement("h2"); h2.textContent = p.title; card.appendChild(h2);
  const legend = document.createElement("div"); legend.className = "legend";
  for (const s of SERIES) {
    const item = document.createElement("span");
    const key = document.createElement("i"); key.className = "key"; key.style.background = s.color;
    item.append(key, document.createTextNode(s.label));
    legend.appendChild(item);
  }
  card.appendChild(legend);
  host.appendChild(card);

  const vals = DATA.flatMap(d => SERIES.map(s => val(d, p, s))).filter(v => v != null);
  const max = niceMax(Math.max(0, ...vals));
  const y = v => M.top + (H - M.top - M.bottom) * (1 - v / max);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `${p.title}: hourly average and peak over time` }, card);

  for (let k = 0; k <= 4; k++) {               // recessive hairline grid, clean ticks
    const v = max * k / 4, yy = y(v);
    el("line", { x1: M.left, x2: W - M.right, y1: yy, y2: yy,
      stroke: k === 0 ? "var(--baseline)" : "var(--grid)", "stroke-width": 1 }, svg);
    el("text", { x: M.left - 8, y: yy + 4, "text-anchor": "end" }, svg).textContent =
      (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(1)) + (p.unit === "%" ? "%" : "");
  }
  for (const [t, i] of xTicks)
    el("text", { x: x(i), y: H - 6, "text-anchor": "middle" }, svg).textContent = fmtTick(t);
  if (p.ref && max >= 100) {   // 100% = exactly the request (ratios) or all capacity
    el("line", { x1: M.left, x2: W - M.right, y1: y(100), y2: y(100),
      stroke: "var(--text-muted)", "stroke-width": 1 }, svg);
    // label above the line, or below it when the line is the top of the axis
    el("text", { x: M.left + 4, y: max === 100 ? y(100) + 13 : y(100) - 4 }, svg).textContent = p.ref;
  }
  if (!vals.length)
    el("text", { x: (M.left + W - M.right) / 2, y: H / 2, "text-anchor": "middle", class: "empty" }, svg)
      .textContent = "No data in this range";

  const ends = [];
  for (const s of SERIES) {
    // missing values (no sar data, nothing requested) break the line into a gap
    let d = "", pen = false, last = null;
    DATA.forEach((row, i) => {
      const v = val(row, p, s);
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true; last = v;
    });
    if (!d) continue;
    el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    // isolated points (a value between two gaps) would be invisible as a path
    DATA.forEach((row, i) => {
      const v = val(row, p, s);
      if (v != null && val(DATA[i - 1] || {}, p, s) == null && val(DATA[i + 1] || {}, p, s) == null)
        el("circle", { cx: x(i), cy: y(v), r: 2, fill: s.color }, svg);
    });
    ends.push({ s, yy: y(last) });
  }
  // direct labels at the line ends, nudged upward (away from the x axis) so they
  // never overlap; on a tie peak sits above avg
  ends.sort((a, b) => b.yy - a.yy || (a.s.key === "avg" ? -1 : 1));
  for (let k = 1; k < ends.length; k++) ends[k].yy = Math.min(ends[k].yy, ends[k - 1].yy - 14);
  for (const e of ends)
    el("text", { x: W - M.right + 6, y: e.yy + 4, class: "dl" }, svg).textContent = e.s.short;

  const cross = el("line", { y1: M.top, y2: H - M.bottom, stroke: "var(--baseline)", "stroke-width": 1,
    visibility: "hidden" }, svg);
  const dots = SERIES.map(s => el("circle", { r: 4, fill: s.color, stroke: "var(--surface-1)",
    "stroke-width": 2, visibility: "hidden" }, svg));
  const hit = el("rect", { x: M.left, y: 0, width: W - M.left - M.right, height: H, fill: "transparent",
    tabindex: 0 }, svg);
  const tip = document.createElement("div"); tip.className = "tip"; card.appendChild(tip);
  return { p, svg, y, cross, dots, hit, tip, card };
});

// one shared crosshair: hovering any panel shows the same hour in all of them
function show(i) {
  const d = DATA[i], xx = x(i);
  for (const P of panels) {
    P.cross.setAttribute("x1", xx); P.cross.setAttribute("x2", xx); P.cross.setAttribute("visibility", "visible");
    SERIES.forEach((s, k) => {
      const v = val(d, P.p, s);
      P.dots[k].setAttribute("visibility", v == null ? "hidden" : "visible");
      if (v != null) { P.dots[k].setAttribute("cx", xx); P.dots[k].setAttribute("cy", P.y(v)); }
    });
    P.tip.replaceChildren();
    const when = document.createElement("div"); when.className = "when"; when.textContent = fmtHour(times[i]);
    P.tip.appendChild(when);
    for (const s of [...SERIES].reverse()) {
      const row = document.createElement("div"); row.className = "row";
      const key = document.createElement("i"); key.className = "key"; key.style.background = s.color;
      const b = document.createElement("b"); b.textContent = fmtVal(val(d, P.p, s), P.p);
      const lab = document.createElement("span"); lab.textContent = s.label + fmtAbs(d, P.p, s);
      row.append(key, b, lab); P.tip.appendChild(row);
    }
    if (d.unknown > 0 && !P.p.id.startsWith("srv_")) {
      const n = document.createElement("div"); n.className = "note";
      n.textContent = `${d.unknown} session${d.unknown > 1 ? "s" : ""} with unknown limits (requested counted as 0)`;
      P.tip.appendChild(n);
    }
    const scale = P.svg.getBoundingClientRect().width / W;
    P.tip.style.display = "block";
    const left = 16 + xx * scale, tw = P.tip.offsetWidth;
    P.tip.style.left = `${left + 12 + tw > P.card.clientWidth ? left - 12 - tw : left + 12}px`;
    P.tip.style.top = "48px";
  }
}
function hide() {
  for (const P of panels) {
    P.cross.setAttribute("visibility", "hidden"); P.dots.forEach(d => d.setAttribute("visibility", "hidden"));
    P.tip.style.display = "none";
  }
}
let focusIdx = DATA.length - 1;
for (const P of panels) {
  const idxAt = ev => {
    const box = P.svg.getBoundingClientRect(), vx = (ev.clientX - box.left) * W / box.width;
    return Math.max(0, Math.min(DATA.length - 1,
      Math.round((vx - M.left) / ((W - M.left - M.right) / Math.max(1, DATA.length - 1)))));
  };
  P.hit.addEventListener("pointermove", ev => { focusIdx = idxAt(ev); show(focusIdx); });
  P.hit.addEventListener("pointerleave", hide);
  P.hit.addEventListener("focus", () => show(focusIdx));
  P.hit.addEventListener("blur", hide);
  P.hit.addEventListener("keydown", ev => {
    if (ev.key === "ArrowLeft") focusIdx = Math.max(0, focusIdx - 1);
    else if (ev.key === "ArrowRight") focusIdx = Math.min(DATA.length - 1, focusIdx + 1);
    else return;
    ev.preventDefault(); show(focusIdx);
  });
}

// table view: every value reachable without hovering
const table = document.getElementById("table");
const head = table.insertRow();
const headers = ["Hour (UTC)", ...PANELS.flatMap(p => SERIES.map(s => `${p.title} – ${s.short}`)), "Unknown limits"];
for (const h of headers) { const th = document.createElement("th"); th.textContent = h; head.appendChild(th); }
DATA.forEach((d, i) => {
  const r = table.insertRow();
  r.insertCell().textContent = fmtHour(times[i]);
  for (const p of PANELS) for (const s of SERIES) {
    const v = val(d, p, s);
    r.insertCell().textContent = v == null ? "–" : v.toFixed(p.decimals);
  }
  r.insertCell().textContent = d.unknown;
});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Plot run.py's hourly CPU / memory avg and peak as HTML.")
    ap.add_argument("csv", help="run.py output CSV, or - for stdin")
    ap.add_argument("-o", "--output", default="usage.html", help="HTML file to write (default: usage.html)")
    ap.add_argument("--title", default="Workbench resources", help="chart page title")
    ap.add_argument("--total-cpus", type=float, metavar="N",
                    help="total CPUs available; show CPU panels as %% of this")
    ap.add_argument("--total-mem-gb", type=float, metavar="GB",
                    help="total memory available in GB; show memory panels as %% of this")
    args = ap.parse_args()
    for name in ("total_cpus", "total_mem_gb"):
        if getattr(args, name) is not None and getattr(args, name) <= 0:
            ap.error(f"--{name.replace('_', '-')} must be positive")
    if args.csv == "-":
        rows, panels = load(sys.stdin)
    else:
        with open(args.csv, newline="", encoding="utf-8") as f:
            rows, panels = load(f)
    relative(rows, panels, args.total_cpus, args.total_mem_gb)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(render(rows, panels, args.title))
    print(f"wrote {args.output} ({len(rows)} hours, {len(panels)} panels)", file=sys.stderr)


if __name__ == "__main__":
    main()
