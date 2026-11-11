#!/usr/bin/env python3
"""minimon_history - a per-minute temperature log, and a report on it.

minimon-daemon.py hands History one sensor snapshot a second. History folds
each wall-clock minute into one CSV row - the average and the peak of every
temperature, plus the average CPU and GPU load, so heat from hard work can be
told apart from heat at idle - appended to a monthly file:

    ~/.local/share/minimon/temps-YYYY-MM.csv

A month is about 3 MB; files older than KEEP_MONTHS are deleted as each new
month starts. Run this file to see how hot the machine has been running:

    python3 minimon_history.py              # last 7 days
    python3 minimon_history.py --days 30

Standard library only, and nothing OS-specific.
"""
import argparse
import csv
import glob
import os
import re
import sys
import time

LOG_DIR = os.path.join(os.environ.get("XDG_DATA_HOME")
                       or os.path.expanduser("~/.local/share"), "minimon")
KEEP_MONTHS = 13
FILE_RE = re.compile(r"^temps-(\d{4}-\d{2})\.csv$")

# Snapshot key -> CSV column, and whether the minute's peak is kept as well.
SAMPLES = (
    ("cpu_t", "cpu_temp", True), ("cpu", "cpu_load", False),
    ("gpu_t", "gpu_temp", True), ("gpu", "gpu_load", False),
    ("disk_t", "ssd_temp", True),
    ("ram_t", "ram_temp", True),
)
COLUMNS = ["time"] + [c for _, col, peak in SAMPLES
                      for c in ((col, col + "_max") if peak else (col,))]
TEMP_COLUMNS = [col for _, col, peak in SAMPLES if peak]

# What the report judges, with its "warm" and "hot" levels in °C. Warm is
# fine by spec but leaves the cooling little headroom; hot is close to where
# the part starts slowing itself down to protect itself (CPUs ~95-100 °C,
# GPUs ~90-95 °C, NVMe SSDs ~80-85 °C, DDR5 85 °C).
PARTS = (  # label, temperature column, load column, warm, hot
    ("CPU", "cpu_temp", "cpu_load", 80, 90),
    ("GPU", "gpu_temp", "gpu_load", 80, 90),
    ("SSD", "ssd_temp", None, 70, 80),
    ("RAM", "ram_temp", None, 65, 80),
)
IDLE, BUSY = 20, 75    # load %, for the "idle" and "busy" averages


class History:
    """Folds one-second snapshots into one CSV row per wall-clock minute."""

    def __init__(self, log_dir=LOG_DIR):
        self.dir = log_dir
        self.minute = None
        self.sum, self.n, self.peak = {}, {}, {}
        self.failed = False

    def add(self, d, now=None):
        minute = int((time.time() if now is None else now) // 60)
        if minute != self.minute:
            self.flush()
            self.minute = minute
        for key, col, _ in SAMPLES:
            v = d.get(key)
            if v is None:
                continue
            self.sum[col] = self.sum.get(col, 0.0) + v
            self.n[col] = self.n.get(col, 0) + 1
            self.peak[col] = max(self.peak.get(col, v), v)

    def flush(self):
        """Write out the minute gathered so far, if it has a temperature."""
        sums, n, peaks = self.sum, self.n, self.peak
        self.sum, self.n, self.peak = {}, {}, {}
        if not any(col in n for col in TEMP_COLUMNS):
            return
        when = time.localtime(self.minute * 60)
        row = {"time": time.strftime("%Y-%m-%d %H:%M", when)}
        for _, col, peak in SAMPLES:
            if col in n:
                row[col] = "%.1f" % (sums[col] / n[col])
                if peak:
                    row[col + "_max"] = "%.1f" % peaks[col]
        path = os.path.join(self.dir, time.strftime("temps-%Y-%m.csv", when))
        try:
            os.makedirs(self.dir, exist_ok=True)
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", newline="") as fh:
                w = csv.DictWriter(fh, COLUMNS)
                if new:
                    w.writeheader()
                w.writerow(row)
            if new:
                prune(self.dir, when)
            self.failed = False
        except OSError as e:
            if not self.failed:     # say so once; never take the daemon down
                print("minimon: cannot write the temperature log: %s" % e,
                      file=sys.stderr)
                self.failed = True


def prune(log_dir, when):
    """Delete the monthly files that fell out of the KEEP_MONTHS window."""
    idx = when.tm_year * 12 + when.tm_mon - 1 - (KEEP_MONTHS - 1)
    oldest = "%04d-%02d" % (idx // 12, idx % 12 + 1)
    for name in os.listdir(log_dir):
        m = FILE_RE.match(name)
        if m and m.group(1) < oldest:
            os.unlink(os.path.join(log_dir, name))


# ---- report ------------------------------------------------------------

def num(row, col):
    try:
        return float(row.get(col) or "")
    except ValueError:
        return None


def mean(vals):
    return sum(vals) / len(vals) if vals else None


def load_rows(log_dir, since):
    """Every logged minute at or after `since` ("YYYY-MM-DD HH:MM")."""
    rows = []
    for path in sorted(glob.glob(os.path.join(log_dir, "temps-*.csv"))):
        m = FILE_RE.match(os.path.basename(path))
        if not m or m.group(1) < since[:7]:
            continue
        try:
            with open(path, newline="") as fh:
                rows += [r for r in csv.DictReader(fh)
                         if (r.get("time") or "") >= since]
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return rows


def summarize(rows, col, load_col, warm, hot):
    temps, peaks, idle, busy, warm_loads = [], [], [], [], []
    for r in rows:
        t = num(r, col)
        if t is None:
            continue
        temps.append(t)
        p = num(r, col + "_max")
        peaks.append(t if p is None else p)
        ld = num(r, load_col) if load_col else None
        if ld is not None:
            if ld < IDLE:
                idle.append(t)
            elif ld >= BUSY:
                busy.append(t)
            if t >= warm:
                warm_loads.append(ld)
    if not temps:
        return None
    n = len(temps)
    warm_n = sum(t >= warm for t in temps)
    hot_n = sum(t >= hot for t in temps)
    if n < 60:
        verdict = "too little data"
    elif hot_n > max(5, 0.01 * n):
        verdict = "HOT"
    elif hot_n or warm_n > 0.10 * n:
        verdict = "warm"
    else:
        verdict = "ok"
    return {"n": n, "avg": mean(temps), "idle": mean(idle), "busy": mean(busy),
            "peak": max(peaks), "warm": warm_n, "hot": hot_n,
            "warm_load": mean(warm_loads), "verdict": verdict}


def deg(v, width=5):
    return ("%.0f°C" % v if v is not None else "-").rjust(width)


def dur(minutes):
    return "%dm" % minutes if minutes < 60 else "%dh %02dm" % divmod(minutes, 60)


def note(label, s, warm, hot):
    """A sentence on a part that ran warm or hot, else None."""
    if s["verdict"] == "HOT":
        text = ("%s averaged %d°C or more for %s (%.0f%% of the time), close "
                "to where it slows itself down to protect itself."
                % (label, hot, dur(s["hot"]), 100.0 * s["hot"] / s["n"]))
    elif s["verdict"] == "warm":
        text = ("%s averaged %d°C or more for %s (%.0f%% of the time): within "
                "spec, but its cooling has little headroom left."
                % (label, warm, dur(s["warm"]), 100.0 * s["warm"] / s["n"]))
    else:
        return None
    if s["warm_load"] is not None:
        if s["warm_load"] < 50:
            text += (" It ran that hot at only %.0f%% average load, which "
                     "points at the cooling (dust, a slow or failed fan, dried "
                     "thermal paste, airflow) rather than at hard work."
                     % s["warm_load"])
        else:
            text += (" That was under heavy load (%.0f%% average), so it is "
                     "the cooler running at its limit." % s["warm_load"])
    elif s["verdict"] == "HOT":
        text += " Give it more airflow or a heatsink."
    return text


def report(rows, days, log_dir, out=sys.stdout):
    def say(line=""):
        print(line.rstrip(), file=out)

    stats = [(label, col, warm, hot, summarize(rows, col, load, warm, hot))
             for label, col, load, warm, hot in PARTS]
    stats = [s for s in stats if s[4]]
    times = sorted(r["time"] for r in rows)
    logged = sum(any(num(r, c) is not None for c in TEMP_COLUMNS) for r in rows)

    say("Temperatures over the last %g days: %.1f h logged, %s to %s"
        % (days, logged / 60.0, times[0], times[-1]))
    say()
    say("part    avg   idle   busy   peak     warm      hot   verdict")
    for label, _, _, _, s in stats:
        say("%-4s  %s  %s  %s  %s  %7s  %7s   %s" % (
            label, deg(s["avg"]), deg(s["idle"]), deg(s["busy"]),
            deg(s["peak"]), dur(s["warm"]), dur(s["hot"]), s["verdict"]))
    say()
    say("avg, idle, busy: average temperature overall, at under %d%% load, "
        "and at %d%%+ load" % (IDLE, BUSY))
    say("peak: the hottest single reading")
    say("warm, hot: total time the per-minute average was at or above: "
        + ", ".join("%s %d/%d°C" % (label, warm, hot)
                    for label, _, warm, hot, _ in stats))

    notes = [note(label, s, warm, hot) for label, _, warm, hot, s in stats]
    notes = [n for n in notes if n]
    if notes:
        say()
        for n in notes:
            say(n)
    elif all(s["verdict"] == "ok" for *_, s in stats):
        say()
        say("Everything stayed clear of its limits.")

    days_seen = {}
    for r in rows:
        days_seen.setdefault(r["time"][:10], []).append(r)
    say()
    say("per day     hours" + "".join("%11s/peak" % (label + " avg")
                                     for label, *_ in stats))
    for day in sorted(days_seen):
        day_rows = days_seen[day]
        cells = ""
        for _, col, *_ in stats:
            temps = [t for t in (num(r, col) for r in day_rows) if t is not None]
            peaks = [p for p in (num(r, col + "_max") for r in day_rows)
                     if p is not None] or temps
            cells += ("%11.0f/%-4.0f" % (mean(temps), max(peaks)) if temps
                      else "%11s/%-4s" % ("-", "-"))
        say("%s  %5.1f%s" % (day, len(day_rows) / 60.0, cells))
    say()
    say("Log: %s" % log_dir)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="How hot has this machine been running? Reads the "
                    "per-minute log that minimon-daemon.py keeps.")
    p.add_argument("--days", type=float, default=7,
                   help="how far back to look (default 7)")
    p.add_argument("--dir", default=LOG_DIR,
                   help="log folder (default %(default)s)")
    a = p.parse_args(argv)
    since = time.strftime("%Y-%m-%d %H:%M",
                          time.localtime(time.time() - a.days * 86400))
    rows = load_rows(a.dir, since)
    if not rows:
        print("No temperature log for the last %g days in %s.\n"
              "minimon-daemon.py writes one line a minute while it runs."
              % (a.days, a.dir))
        return 1
    report(rows, a.days, a.dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
