#!/usr/bin/env python3
"""minimon-bar - minimon's readout in the GNOME top bar.

A compact live label (CPU / GPU / RAM / Claude session) sits in the panel;
the dropdown menu carries the full breakdown the floating widget shows.
Reuses minimon.py for all sensor and Claude-usage plumbing.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppInd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import minimon as mm  # noqa: E402

DEFAULT_FMT = "C{cpu}% {ct}° · G{gpu}% · M{mem}% · CC{cc}%"
GUIDE = "C100% 99° · G100% · M100% · CC100%"


class Bar:
    def __init__(self, args):
        self.args = args
        self.sensors = mm.Sensors()
        self.session_pct = None
        self.claude_lines = []   # (key, text) for the dropdown

        self.ind = AppInd.Indicator.new(
            "minimon-bar", "utilities-system-monitor-symbolic",
            AppInd.IndicatorCategory.SYSTEM_SERVICES)
        self.ind.set_status(AppInd.IndicatorStatus.ACTIVE)
        self.ind.set_label("…", GUIDE)

        self.menu = Gtk.Menu()
        self.mi = {}
        for key in ("cpu", "gpu", "ram", "net", "extra"):
            self.mi[key] = Gtk.MenuItem(label="…")
            self.menu.append(self.mi[key])
        self.menu.append(Gtk.SeparatorMenuItem())
        head = Gtk.MenuItem(label="Claude Code")
        head.set_sensitive(False)
        self.menu.append(head)
        self.usage_items = {}
        self.usage_anchor = Gtk.SeparatorMenuItem()
        self.mi["today"] = Gtk.MenuItem(label="today  --")
        self.menu.append(self.mi["today"])
        self.menu.append(self.usage_anchor)
        launch = Gtk.MenuItem(label="Open floating widget")
        launch.connect("activate", self.spawn_widget)
        self.menu.append(launch)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: self.quit())
        self.menu.append(quit_item)
        self.menu.show_all()
        self.ind.set_menu(self.menu)

        self._last = GLib.get_monotonic_time() / 1e6
        self.tick()
        GLib.timeout_add(int(args.interval * 1000), self.tick)

        self._claude_stop = threading.Event()
        if not args.no_claude:
            threading.Thread(target=self._claude_worker, daemon=True).start()

    # ---- hardware ----------------------------------------------------
    def tick(self):
        now = GLib.get_monotonic_time() / 1e6
        dt, self._last = now - self._last, now
        d = self.sensors.read_all(dt)
        used, total = d["ram"]
        cc = "--" if self.session_pct is None else "%.0f" % self.session_pct
        label = self.args.format.format(
            cpu="%.0f" % d["cpu"],
            ct="%.0f" % d["cpu_t"] if d["cpu_t"] else "--",
            gpu="%.0f" % d["gpu"],
            gt="%.0f" % d["gpu_t"] if d["gpu_t"] else "--",
            mem="%.0f" % (100 * used / total),
            cc=cc)
        self.ind.set_label(label, GUIDE)

        self.mi["cpu"].set_label("CPU  %3.0f%%   %s   %s" % (
            d["cpu"], "%.0f°C" % d["cpu_t"] if d["cpu_t"] else "",
            "%.1f GHz" % d["ghz"] if d["ghz"] else ""))
        self.mi["gpu"].set_label("GPU  %3.0f%%   %s   %s" % (
            d["gpu"], "%.0f°C" % d["gpu_t"] if d["gpu_t"] else "",
            "%.0f W" % d["gpu_w"] if d["gpu_w"] else ""))
        self.mi["ram"].set_label("RAM  %3.0f%%   %.1f / %.0f GB" % (
            100 * used / total, used, total))
        self.mi["net"].set_label("NET  ↓ %s/s   ↑ %s/s" % (
            mm.rate(d["net"][0]).strip(), mm.rate(d["net"][1]).strip()))
        bits = []
        if d["vram"]:
            bits.append("VRAM %.0f/%.0f M" % d["vram"])
        if d["disk_t"]:
            bits.append("SSD %.0f°C" % d["disk_t"])
        self.mi["extra"].set_label("  ·  ".join(bits) or " ")
        return True

    # ---- Claude usage ------------------------------------------------
    def _claude_worker(self):
        stats = mm.ClaudeStats()
        next_api = 0.0
        have_rows = False
        while not self._claude_stop.is_set():
            try:
                local = stats.scan()
            except Exception:
                local = None
            api = err = None
            if time.time() >= next_api:
                api, err = mm.fetch_claude_usage()
                if err and not have_rows:
                    delay = 30
                else:
                    delay = 300 if err else 120
                if api:
                    have_rows = True
                next_api = time.time() + delay
            GLib.idle_add(self._apply_claude, local, api, err)
            self._claude_stop.wait(60)

    def _apply_claude(self, local, api, err):
        if local is not None:
            self.mi["today"].set_label(
                "today  %s calls · %s out tok" % (
                    mm.fmt_count(local["calls"]), mm.fmt_count(local["out"])))
        if err:
            self.session_pct = None
            key = "err"
            item = self.usage_items.get(key)
            if item is None:
                item = self.usage_items[key] = Gtk.MenuItem()
                pos = self.menu.get_children().index(self.usage_anchor)
                self.menu.insert(item, pos)
                item.show()
            item.set_label("limits: %s" % err)
        if api:
            e = self.usage_items.pop("err", None)
            if e is not None:
                self.menu.remove(e)
            for key, label, pct, resets in mm.usage_rows(api):
                if key.startswith("session") or key == "five_hour":
                    self.session_pct = pct
                item = self.usage_items.get(key)
                if item is None:
                    item = self.usage_items[key] = Gtk.MenuItem()
                    pos = self.menu.get_children().index(self.usage_anchor)
                    self.menu.insert(item, pos)
                    item.show()
                text = "%s  %.0f%%" % (label, pct)
                extra = mm.reset_text(resets)
                if extra:
                    text += "   (%s)" % extra
                item.set_label(text)
        return False

    # ---- misc --------------------------------------------------------
    def spawn_widget(self, *_):
        subprocess.Popen([sys.executable, os.path.join(HERE, "minimon.py")])

    def quit(self):
        self._claude_stop.set()
        Gtk.main_quit()


def main():
    p = argparse.ArgumentParser(description="minimon in the GNOME top bar")
    p.add_argument("--interval", type=float, default=2.0, help="refresh seconds")
    p.add_argument("--format", default=DEFAULT_FMT,
                   help="label template; tokens {cpu} {ct} {gpu} {gt} {mem} {cc}")
    p.add_argument("--no-claude", action="store_true",
                   help="hide the Claude Code usage data")
    args = p.parse_args()

    global _lock
    _lock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        _lock.bind("\0minimon-bar-%d" % os.getuid())
    except OSError:
        print("minimon-bar is already running", file=sys.stderr)
        return 0

    bar = Bar(args)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: bar.quit())
    Gtk.main()


if __name__ == "__main__":
    sys.exit(main())
