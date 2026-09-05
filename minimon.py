#!/usr/bin/env python3
"""minimon - an NZXT CAM "mini mode" style always-on-top hardware monitor for Linux.

Pure GTK3 + /proc + /sys. No root, no external deps beyond PyGObject.
Left-drag to move, right-click for the menu, position is remembered.
"""
import argparse
import datetime
import glob
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.request

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

CONFIG = os.path.expanduser("~/.config/minimon.json")

CPU_HWMON = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "acpitz")
GPU_HWMON = ("amdgpu", "nvidia", "i915", "xe")

CSS = """
window { background-color: transparent; }
#card {
  background-color: rgba(17, 18, 23, %(alpha).2f);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 14px;
  padding: 10px 13px 11px 13px;
}
#host  { color: rgba(255,255,255,0.30); font-size: 9px; font-weight: 700; letter-spacing: 1.4px; }
#close { color: rgba(255,255,255,0.28); font-size: 11px; font-weight: 700; }
.name  { color: rgba(255,255,255,0.90); font-size: 11px; font-weight: 700; letter-spacing: 0.5px; }
.sub   { color: rgba(255,255,255,0.45); font-size: 10px;
         font-family: "Ubuntu Mono","DejaVu Sans Mono",monospace; }
.big   { color: #ffffff; font-size: 12px; font-weight: 700;
         font-family: "Ubuntu Mono","DejaVu Sans Mono",monospace; }
.warm  { color: #fbbf24; }
.hot   { color: #f87171; }
.foot  { color: rgba(255,255,255,0.42); font-size: 10px;
         font-family: "Ubuntu Mono","DejaVu Sans Mono",monospace; }
progressbar { padding: 0px; }
progressbar trough {
  min-height: 5px; border: none; border-radius: 3px;
  background-color: rgba(255,255,255,0.09); background-image: none;
}
progressbar progress {
  min-height: 5px; border: none; border-radius: 3px; background-image: none;
}
progressbar.cpu progress { background-color: #7c5cff; }
progressbar.gpu progress { background-color: #22d3ee; }
progressbar.ram progress { background-color: #34d399; }
progressbar.u0 progress { background-color: #e879f9; }
progressbar.u1 progress { background-color: #fb923c; }
progressbar.u2 progress { background-color: #f87171; }
separator { background-color: rgba(255,255,255,0.07); min-height: 1px; }
menu { background-color: #1b1d23; color: #e8e8ea; }
menuitem:hover { background-color: #7c5cff; }
"""


def read(path, cast=str, default=None):
    try:
        with open(path) as fh:
            return cast(fh.read().strip())
    except (OSError, ValueError):
        return default


class Sensors:
    """Discovers sensor paths once, then reads them cheaply every tick."""

    def __init__(self):
        by_name = {}
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name = read(os.path.join(d, "name"))
            if name:
                by_name.setdefault(name, d)
        self.by_name = by_name

        self.cpu_temp = self._temp(CPU_HWMON, ("Tctl", "Package id 0", "Tdie"))
        gpu_dir = next((by_name[n] for n in GPU_HWMON if n in by_name), None)
        self.gpu_temp = self._temp(GPU_HWMON, ("edge", "junction"))
        self.gpu_power = os.path.join(gpu_dir, "power1_input") if gpu_dir else None
        if self.gpu_power and not os.path.exists(self.gpu_power):
            self.gpu_power = None
        self.gpu_busy = next(iter(glob.glob(
            "/sys/class/drm/card*/device/gpu_busy_percent")), None)
        dev = os.path.dirname(self.gpu_busy) if self.gpu_busy else None
        self.vram_used = os.path.join(dev, "mem_info_vram_used") if dev else None
        self.vram_total = os.path.join(dev, "mem_info_vram_total") if dev else None
        self.disk_temp = self._temp(("nvme", "drivetemp"), ("Composite",))
        self.ram_temp = self._temp(("spd5118", "jc42"), ())
        self.freqs = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")

        self._cpu_prev = None
        self._net_prev = None

    def _temp(self, names, labels):
        for n in names:
            d = self.by_name.get(n)
            if not d:
                continue
            for lbl in glob.glob(os.path.join(d, "temp*_label")):
                if read(lbl) in labels:
                    cand = lbl.replace("_label", "_input")
                    if os.path.exists(cand):
                        return cand
            cand = os.path.join(d, "temp1_input")
            if os.path.exists(cand):
                return cand
        return None

    @staticmethod
    def _milli(path):
        v = read(path, int) if path else None
        return v / 1000.0 if v is not None else None

    def cpu_percent(self):
        line = read("/proc/stat", default="")
        vals = [int(x) for x in line.split("\n")[0].split()[1:]]
        idle, total = vals[3] + vals[4], sum(vals)
        prev, self._cpu_prev = self._cpu_prev, (idle, total)
        if not prev:
            return 0.0
        d_total = total - prev[1]
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (1.0 - (idle - prev[0]) / d_total)))

    def cpu_ghz(self):
        vals = [read(f, int) for f in self.freqs]
        vals = [v for v in vals if v]
        if vals:
            return sum(vals) / len(vals) / 1e6
        mhz = [float(l.split(":")[1]) for l in open("/proc/cpuinfo")
               if l.startswith("cpu MHz")]
        return sum(mhz) / len(mhz) / 1000.0 if mhz else None

    @staticmethod
    def mem():
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0])
        total = info["MemTotal"] / 1048576.0
        used = (info["MemTotal"] - info.get("MemAvailable", info["MemFree"])) / 1048576.0
        return used, total

    def net(self, dt):
        rx = tx = 0
        with open("/proc/net/dev") as fh:
            for line in fh.readlines()[2:]:
                iface, _, rest = line.partition(":")
                if iface.strip() in ("lo",) or iface.strip().startswith(
                        ("docker", "veth", "br-", "virbr")):
                    continue
                f = rest.split()
                rx += int(f[0])
                tx += int(f[8])
        prev, self._net_prev = self._net_prev, (rx, tx)
        if not prev or dt <= 0:
            return 0.0, 0.0
        return (rx - prev[0]) / dt, (tx - prev[1]) / dt

    def read_all(self, dt):
        used, total = self.mem()
        vu, vt = read(self.vram_used, int), read(self.vram_total, int)
        rx, tx = self.net(dt)
        pwr = read(self.gpu_power, int)
        return {
            "cpu": self.cpu_percent(),
            "cpu_t": self._milli(self.cpu_temp),
            "ghz": self.cpu_ghz(),
            "gpu": float(read(self.gpu_busy, int, 0) or 0),
            "gpu_t": self._milli(self.gpu_temp),
            "gpu_w": pwr / 1e6 if pwr else None,
            "vram": (vu / 1048576.0, vt / 1048576.0) if vu and vt else None,
            "ram": (used, total),
            "ram_t": self._milli(self.ram_temp),
            "disk_t": self._milli(self.disk_temp),
            "net": (rx, tx),
        }


CRED_FILE = os.path.expanduser("~/.claude/.credentials.json")
PROJECTS = os.path.expanduser("~/.claude/projects")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Legacy top-level windows; only used when the response has no `limits` array.
CLAUDE_WINDOWS = (("five_hour", "Session"), ("seven_day", "Week"),
                  ("seven_day_opus", "Week·Opus"), ("seven_day_sonnet", "Week·Sonnet"))


def _scope_name(lim):
    scope = lim.get("scope") or {}
    for part in ("model", "surface"):
        d = scope.get(part)
        if isinstance(d, dict) and d.get("display_name"):
            return d["display_name"]
    return None


def usage_rows(api):
    """Normalize the usage response into (key, label, percent, resets_at).

    Prefers the `limits` array, which carries every window the in-app
    breakdown shows - including per-model scoped weeks like Fable - and
    falls back to the legacy fixed top-level keys."""
    rows = []
    for lim in (api.get("limits") or []):
        if not isinstance(lim, dict) or lim.get("percent") is None:
            continue
        kind = str(lim.get("kind") or "")
        name = _scope_name(lim)
        if kind == "session":
            label = "Session"
        elif kind == "weekly_all":
            label = "Week"
        elif name:
            label = ("Week·%s" % name) if kind.startswith("weekly") else name
        else:
            label = kind.replace("_", " ").title()
        rows.append((kind + (":" + name if name else ""), label,
                     float(lim["percent"]), lim.get("resets_at")))
    if rows:
        return rows
    for key, label in CLAUDE_WINDOWS:
        d = api.get(key)
        if isinstance(d, dict) and d.get("utilization") is not None:
            rows.append((key, label, float(d["utilization"]),
                         d.get("resets_at")))
    return rows


def fmt_count(n):
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return "%.1f%s" % (n / div, unit)
    return "%d" % n


def reset_text(iso):
    if not iso:
        return ""
    try:
        when = datetime.datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    soon = (when - datetime.datetime.now(when.tzinfo)).total_seconds() < 86400
    return "resets " + when.strftime("%H:%M" if soon else "%a %H:%M")


def fetch_claude_usage():
    """Same endpoint the in-app usage button reads. The OAuth token is read
    fresh from Claude Code's own credentials file on every call and is sent
    nowhere except api.anthropic.com."""
    fake = os.environ.get("MINIMON_USAGE_JSON")
    if fake:
        try:
            return json.load(open(fake)), None
        except (OSError, ValueError):
            return None, "bad fake"
    try:
        cred = json.load(open(CRED_FILE)).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return None, "no login"
    token = cred.get("accessToken")
    if not token:
        return None, "no login"
    if cred.get("expiresAt", 0) / 1000.0 < time.time():
        return None, "token stale"
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp), None
    except Exception as exc:  # noqa: BLE001 - any failure is a soft error
        return None, ("token stale" if getattr(exc, "code", None) == 401
                      else "offline")


class ClaudeStats:
    """Incrementally tallies today's usage from Claude Code transcripts.

    Keeps a byte offset per JSONL file so each scan only reads appended
    lines; requestIds dedup the multi-line entries a single API call emits.
    """

    def __init__(self):
        self.day = None
        self.offsets = {}
        self.seen = set()
        self.calls = 0
        self.out = 0

    def scan(self):
        now = time.localtime()
        day = time.strftime("%Y-%m-%d", now)
        midnight = time.mktime(
            (now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
        if day != self.day:
            self.day = day
            self.offsets, self.seen = {}, set()
            self.calls = self.out = 0
        for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
            try:
                if os.path.getmtime(path) < midnight:
                    continue
                off = self.offsets.get(path, 0)
                if os.path.getsize(path) <= off:
                    continue
                with open(path, "rb") as fh:
                    fh.seek(off)
                    data = fh.read()
            except OSError:
                continue
            nl = data.rfind(b"\n")
            if nl < 0:
                continue
            self.offsets[path] = off + nl + 1
            for raw in data[:nl].split(b"\n"):
                self._line(raw, midnight)
        return {"calls": self.calls, "out": self.out}

    def _line(self, raw, midnight):
        try:
            d = json.loads(raw)
            if d.get("type") != "assistant":
                return
            msg = d.get("message") or {}
            usage, ts = msg.get("usage"), d.get("timestamp")
            if not usage or not ts:
                return
            when = datetime.datetime.fromisoformat(
                ts.replace("Z", "+00:00")).timestamp()
            if when < midnight:
                return
            key = d.get("requestId") or msg.get("id")
            if not key or key in self.seen:
                return
            self.seen.add(key)
            self.calls += 1
            self.out += int(usage.get("output_tokens") or 0)
        except (ValueError, KeyError, TypeError):
            return


def rate(b):
    for unit, div in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if b >= div:
            return "%5.1f%s" % (b / div, unit)
    return "%5.0fB" % b


class Row:
    def __init__(self, name, css_class):
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.name = Gtk.Label(label=name, xalign=0)
        self.name.get_style_context().add_class("name")
        self.sub = Gtk.Label(label="", xalign=1)
        self.sub.get_style_context().add_class("sub")
        self.val = Gtk.Label(label="--", xalign=1)
        self.val.get_style_context().add_class("big")
        head.pack_start(self.name, False, False, 0)
        head.pack_end(self.val, False, False, 0)
        head.pack_end(self.sub, True, True, 8)
        self.bar = Gtk.ProgressBar(show_text=False)
        self.bar.get_style_context().add_class(css_class)
        self.box.pack_start(head, False, False, 0)
        self.box.pack_start(self.bar, False, False, 0)

    def update(self, frac, value, sub, temp=None):
        self.bar.set_fraction(max(0.0, min(1.0, frac)))
        self.val.set_text(value)
        self.sub.set_text(sub)
        ctx = self.sub.get_style_context()
        for c in ("warm", "hot"):
            ctx.remove_class(c)
        if temp is not None:
            if temp >= 85:
                ctx.add_class("hot")
            elif temp >= 70:
                ctx.add_class("warm")


class Monitor(Gtk.Window):
    def __init__(self, args):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.args = args
        self.sensors = Sensors()
        self.cfg = {}
        if os.path.exists(CONFIG):
            try:
                self.cfg = json.load(open(CONFIG))
            except (OSError, ValueError):
                pass

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()
        self.set_title("minimon")
        self.set_size_request(args.width, -1)

        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

        css = Gtk.CssProvider()
        css.load_from_data((CSS % {"alpha": args.opacity}).encode())
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        card.set_name("card")
        self.add(card)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        host = Gtk.Label(label=socket.gethostname().upper(), xalign=0)
        host.set_name("host")
        close = Gtk.Label(label="✕")
        close.set_name("close")
        close_box = Gtk.EventBox()
        close_box.add(close)
        close_box.connect("button-press-event", lambda *_: self.quit())
        header.pack_start(host, False, False, 0)
        header.pack_end(close_box, False, False, 0)
        card.pack_start(header, False, False, 0)

        self.rows = {
            "cpu": Row("CPU", "cpu"),
            "gpu": Row("GPU", "gpu"),
            "ram": Row("RAM", "ram"),
        }
        for r in self.rows.values():
            card.pack_start(r.box, False, False, 0)

        card.pack_start(Gtk.Separator(), False, False, 2)
        grid = Gtk.Grid(column_homogeneous=True, row_spacing=3)
        self.foot = {}
        for i, (key, align) in enumerate((("ghz", 0), ("pwr", 1), ("vram", 0),
                                          ("disk", 1), ("dn", 0), ("up", 1))):
            lbl = Gtk.Label(label="", xalign=float(align))
            lbl.get_style_context().add_class("foot")
            lbl.set_hexpand(True)
            grid.attach(lbl, i % 2, i // 2, 1, 1)
            self.foot[key] = lbl
        card.pack_start(grid, False, False, 0)

        self.claude_rows = {}
        if not args.no_claude:
            card.pack_start(Gtk.Separator(), False, False, 2)
            chead = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            clbl = Gtk.Label(label="CLAUDE CODE", xalign=0)
            clbl.set_name("host")
            self.cstat = Gtk.Label(label="", xalign=1)
            self.cstat.get_style_context().add_class("foot")
            chead.pack_start(clbl, False, False, 0)
            chead.pack_end(self.cstat, False, False, 0)
            card.pack_start(chead, False, False, 0)
            self.claude_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=9)
            card.pack_start(self.claude_box, False, False, 0)
            self.ctoday = Gtk.Label(label="today  --", xalign=0)
            self.ctoday.get_style_context().add_class("foot")
            card.pack_start(self.ctoday, False, False, 0)

        self.menu = Gtk.Menu()
        for label, cb in (("Move to top-right", self.snap_tr),
                          ("Move to bottom-right", self.snap_br),
                          ("Quit", lambda *_: self.quit())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            self.menu.append(item)
        self.menu.show_all()

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self.on_click)
        self.connect("delete-event", lambda *_: self.quit())
        self.connect("key-press-event", self.on_key)

        self.show_all()
        self.place()
        self._last = GLib.get_monotonic_time() / 1e6
        self.tick()
        GLib.timeout_add(int(args.interval * 1000), self.tick)

        self._claude_stop = threading.Event()
        if not args.no_claude:
            threading.Thread(target=self._claude_worker, daemon=True).start()

    # ---- placement -------------------------------------------------
    def place(self):
        if self.args.pos:
            x, y = (int(v) for v in self.args.pos.split(","))
        elif "x" in self.cfg:
            x, y = self.cfg["x"], self.cfg["y"]
        else:
            return self.snap_tr()
        self.move(x, y)

    def _work_area(self):
        d = self.get_display()
        mon = d.get_monitor_at_window(self.get_window()) or d.get_monitor(0)
        return mon.get_workarea()

    def snap_tr(self, *_):
        wa, (w, h) = self._work_area(), self.get_size()
        self.move(wa.x + wa.width - w - 16, wa.y + 16)

    def snap_br(self, *_):
        wa, (w, h) = self._work_area(), self.get_size()
        self.move(wa.x + wa.width - w - 16, wa.y + wa.height - h - 16)

    # ---- interaction -----------------------------------------------
    def on_click(self, _w, ev):
        if ev.button == 1:
            self.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
            return True
        if ev.button == 3:
            self.menu.popup_at_pointer(ev)
            return True
        return False

    def on_key(self, _w, ev):
        if ev.keyval in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.quit()
        return False

    def quit(self):
        self._claude_stop.set()
        x, y = self.get_position()
        try:
            json.dump({"x": x, "y": y}, open(CONFIG, "w"))
        except OSError:
            pass
        Gtk.main_quit()

    # ---- Claude Code usage -----------------------------------------
    def _claude_worker(self):
        stats = ClaudeStats()
        next_api = 0.0
        while not self._claude_stop.is_set():
            try:
                local = stats.scan()
            except Exception:
                local = None
            api = err = None
            if time.time() >= next_api:
                api, err = fetch_claude_usage()
                if err and not self.claude_rows:
                    delay = 30    # at login the network may still be coming up
                else:
                    delay = 300 if err else 120
                next_api = time.time() + delay
            GLib.idle_add(self._apply_claude, local, api, err)
            self._claude_stop.wait(60)

    def _apply_claude(self, local, api, err):
        if local is not None:
            self.ctoday.set_text("today  %s calls · %s out tok"
                                 % (fmt_count(local["calls"]),
                                    fmt_count(local["out"])))
        if err is not None:
            self.cstat.set_text(err)
        elif api is not None:
            self.cstat.set_text("")
        if api:
            colors = ("u0", "u1", "u2")
            for i, (key, label, pct, resets) in enumerate(usage_rows(api)):
                row = self.claude_rows.get(key)
                if row is None:
                    row = self.claude_rows[key] = Row(
                        label, colors[min(i, len(colors) - 1)])
                    self.claude_box.pack_start(row.box, False, False, 0)
                    row.box.show_all()
                u = max(0.0, min(100.0, pct))
                row.update(u / 100.0, "%3.0f%%" % u, reset_text(resets), u)
        return False

    # ---- refresh ---------------------------------------------------
    def tick(self):
        now = GLib.get_monotonic_time() / 1e6
        dt, self._last = now - self._last, now
        d = self.sensors.read_all(dt)

        t = d["cpu_t"]
        self.rows["cpu"].update(d["cpu"] / 100.0, "%3.0f%%" % d["cpu"],
                                "%.0f°C" % t if t else "", t)
        t = d["gpu_t"]
        self.rows["gpu"].update(d["gpu"] / 100.0, "%3.0f%%" % d["gpu"],
                                "%.0f°C" % t if t else "", t)
        used, total = d["ram"]
        ram_sub = "%.1f/%.0f GB" % (used, total)
        if d.get("ram_t"):
            ram_sub = "%.0f°C · %s" % (d["ram_t"], ram_sub)
        self.rows["ram"].update(used / total, "%3.0f%%" % (100 * used / total),
                                ram_sub, d.get("ram_t"))

        self.foot["ghz"].set_text("%.1f GHz" % d["ghz"] if d["ghz"] else "")
        self.foot["pwr"].set_text("%.0f W" % d["gpu_w"] if d["gpu_w"] else "")
        self.foot["vram"].set_text("VRAM %.0fM" % d["vram"][0] if d["vram"] else "")
        self.foot["disk"].set_text("SSD %.0f°C" % d["disk_t"] if d["disk_t"] else "")
        self.foot["dn"].set_text("↓ %s/s" % rate(d["net"][0]))
        self.foot["up"].set_text("↑ %s/s" % rate(d["net"][1]))
        return True


def main():
    p = argparse.ArgumentParser(description="CAM mini-mode style system monitor")
    p.add_argument("--interval", type=float, default=1.0, help="refresh seconds")
    p.add_argument("--opacity", type=float, default=0.92, help="card opacity 0-1")
    p.add_argument("--width", type=int, default=232, help="widget width in px")
    p.add_argument("--pos", help="initial position as X,Y")
    p.add_argument("--no-claude", action="store_true",
                   help="hide the Claude Code usage section")
    args = p.parse_args()

    # Single instance: an abstract-namespace socket is released automatically
    # when the process dies, so autostart can never stack a second widget.
    global _lock
    _lock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        _lock.bind("\0minimon-%d" % os.getuid())
    except OSError:
        print("minimon is already running", file=sys.stderr)
        return 0

    win = Monitor(args)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: win.quit())
    Gtk.main()


if __name__ == "__main__":
    sys.exit(main())
