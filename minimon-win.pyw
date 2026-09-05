#!/usr/bin/env python3
"""minimon for Windows 11 - NZXT CAM "mini mode" style floating monitor.

Pure stdlib (tkinter + ctypes). CPU load, RAM and network come from the
Win32 API. Temperatures, GPU load/power, VRAM and fan-adjacent extras come
from LibreHardwareMonitor when it is running (Options -> Remote Web Server,
default port 8085; falls back to LHM's WMI namespace). Claude Code usage
comes from minimon_core, reading the same files the CLI uses.

Run with pythonw minimon-win.pyw (or the packaged minimon-win-x64.exe).
--demo renders with synthetic sensor data on any OS.
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import minimon_core as core

IS_WIN = sys.platform == "win32"
CONFIG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                      "minimon", "config.json")
LHM_URL = "http://localhost:8085/data.json"

BG = "#111217"
TRANS = "#0b0c10"          # magic color rendered fully transparent
FG = "#ffffff"
DIM = "#9a9aa5"
WARM, HOT = "#fbbf24", "#f87171"
TRACK = "#2a2b33"
COLORS = {"cpu": "#7c5cff", "gpu": "#22d3ee", "ram": "#34d399",
          "u0": "#e879f9", "u1": "#fb923c", "u2": "#f87171"}
W, PAD, BARH = 252, 14, 5


def short_reset(iso):
    r = core.reset_text(iso)
    return r[7:] if r.startswith("resets ") else r


# ---------------------------------------------------------------- sensors --
class WinSensors:
    """CPU/RAM/net via Win32; temps and GPU via LibreHardwareMonitor."""

    def __init__(self):
        self._prev_cpu = None
        self._prev_net = None
        self._lhm_mode = None       # "http", "wmi" or "none"
        self._lhm = {}
        self._lhm_ts = 0.0

    # --- Win32 basics ---
    def cpu_percent(self):
        k = ctypes.windll.kernel32
        idle, kern, user = (ctypes.c_uint64(), ctypes.c_uint64(),
                            ctypes.c_uint64())
        if not k.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                ctypes.byref(user)):
            return 0.0
        cur = (idle.value, kern.value + user.value)
        prev, self._prev_cpu = self._prev_cpu, cur
        if not prev:
            return 0.0
        d_total = cur[1] - prev[1]
        d_idle = cur[0] - prev[0]
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (1 - d_idle / d_total)))

    @staticmethod
    def mem():
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32),
                        ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        total = st.ullTotalPhys / (1 << 30)
        used = (st.ullTotalPhys - st.ullAvailPhys) / (1 << 30)
        return used, total

    def net(self, dt):
        # GetIfTable2 walk kept minimal: sum octets on operational interfaces
        try:
            iphlpapi = ctypes.windll.iphlpapi
        except OSError:
            return 0.0, 0.0

        class MIB_IF_ROW2(ctypes.Structure):
            _fields_ = [("InterfaceLuid", ctypes.c_uint64),
                        ("InterfaceIndex", ctypes.c_uint32),
                        ("_pad", ctypes.c_ubyte * 1220),
                        ("InOctets", ctypes.c_uint64),
                        ("InUcastPkts", ctypes.c_uint64),
                        ("InNUcastPkts", ctypes.c_uint64),
                        ("InDiscards", ctypes.c_uint64),
                        ("InErrors", ctypes.c_uint64),
                        ("InUnknownProtos", ctypes.c_uint64),
                        ("InUcastOctets", ctypes.c_uint64),
                        ("InMulticastOctets", ctypes.c_uint64),
                        ("InBroadcastOctets", ctypes.c_uint64),
                        ("OutOctets", ctypes.c_uint64),
                        ("_tail", ctypes.c_ubyte * 80)]

        class MIB_IF_TABLE2(ctypes.Structure):
            _fields_ = [("NumEntries", ctypes.c_uint32),
                        ("_pad", ctypes.c_ubyte * 4),
                        ("Table", MIB_IF_ROW2 * 1)]

        ptr = ctypes.POINTER(MIB_IF_TABLE2)()
        if iphlpapi.GetIfTable2(ctypes.byref(ptr)) != 0:
            return 0.0, 0.0
        rx = tx = 0
        try:
            n = ptr.contents.NumEntries
            rows = ctypes.cast(
                ctypes.addressof(ptr.contents.Table),
                ctypes.POINTER(MIB_IF_ROW2 * n)).contents
            for row in rows:
                rx += row.InOctets
                tx += row.OutOctets
        finally:
            iphlpapi.FreeMibTable(ptr)
        cur = (rx, tx)
        prev, self._prev_net = self._prev_net, cur
        if not prev or dt <= 0:
            return 0.0, 0.0
        return (max(0, cur[0] - prev[0]) / dt, max(0, cur[1] - prev[1]) / dt)

    # --- LibreHardwareMonitor ---
    def _lhm_http(self):
        with urllib.request.urlopen(LHM_URL, timeout=2) as r:
            tree = json.load(r)
        flat = {}

        def walk(node, path):
            text = node.get("Text", "")
            for ch in node.get("Children", []):
                walk(ch, path + [text])
            if node.get("SensorId") or (node.get("Value") and not node.get("Children")):
                flat[" / ".join(path[1:] + [text])] = node.get("Value", "")
        walk(tree, [])
        return flat

    def _lhm_wmi(self):
        ps = ("Get-CimInstance -Namespace root/LibreHardwareMonitor "
              "-ClassName Sensor | Select-Object Name,SensorType,Value | "
              "ConvertTo-Json -Compress")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000)   # CREATE_NO_WINDOW
        rows = json.loads(out.stdout or "[]")
        if isinstance(rows, dict):
            rows = [rows]
        return {"%s [%s]" % (r["Name"], r["SensorType"]): r["Value"]
                for r in rows}

    @staticmethod
    def _num(text):
        if isinstance(text, (int, float)):
            return float(text)
        text = str(text).replace(",", ".")
        num = ""
        for ch in text:
            if ch.isdigit() or ch in ".-":
                num += ch
            elif num:
                break
        try:
            return float(num)
        except ValueError:
            return None

    def _lhm_pick(self, want_type, *needles, reject=()):
        best = None
        for name, value in self._lhm.items():
            low = name.lower()
            if want_type and want_type not in low:
                continue
            if any(r in low for r in reject):
                continue
            if all(n in low for n in needles):
                v = self._num(value)
                if v is not None and (best is None or v > best):
                    best = v
        return best

    def refresh_lhm(self):
        if self._lhm_mode == "none" and time.time() - self._lhm_ts < 60:
            return
        for mode, fn in (("http", self._lhm_http), ("wmi", self._lhm_wmi)):
            if self._lhm_mode not in (None, "none", mode):
                continue
            try:
                self._lhm = fn()
                self._lhm_mode = mode
                self._lhm_ts = time.time()
                return
            except Exception:
                continue
        self._lhm_mode = "none"
        self._lhm_ts = time.time()
        self._lhm = {}

    # --- combined snapshot ---
    def read_all(self, dt):
        used, total = self.mem()
        rx, tx = self.net(dt)
        http = self._lhm_mode == "http"
        cpu_t = (self._lhm_pick("temperature", "cpu", reject=("distance",))
                 if not http else
                 self._lhm_pick("", "cpu", "temperature", reject=("distance",))
                 ) if self._lhm else None
        gpu_t = self._lhm_pick("", "gpu", "core") if self._lhm else None
        return {
            "cpu": self.cpu_percent(),
            "cpu_t": self._lhm_pick("", "cpu", "tctl") or
            self._lhm_pick("", "cpu", "package") or cpu_t,
            "ghz": (self._lhm_pick("", "cpu", "clock", reject=("bus",)) or 0)
            / 1000 or None,
            "gpu": self._lhm_pick("", "gpu", "core", "load")
            or self._lhm_pick("", "gpu", "d3d", "3d") or 0.0,
            "gpu_t": gpu_t if gpu_t and gpu_t > 5 else None,
            "gpu_w": self._lhm_pick("", "gpu", "power"),
            "vram": None,
            "ram": (used, total),
            "ram_t": self._lhm_pick("", "memory", "temperature"),
            "disk_t": self._lhm_pick("", "temperature", reject=("cpu", "gpu", "memory", "distance")) if self._lhm_mode == "wmi" else None,
            "net": (rx, tx),
            "lhm": self._lhm_mode or "none",
        }


class DemoSensors:
    """Synthetic data so the UI can be exercised on any OS."""

    def __init__(self):
        self.t0 = time.time()

    def refresh_lhm(self):
        pass

    def read_all(self, _dt):
        import math
        x = time.time() - self.t0
        wob = lambda lo, hi, period, phase=0: lo + (hi - lo) * 0.5 * (
            1 + math.sin(x / period + phase))
        return {"cpu": wob(5, 72, 7), "cpu_t": wob(48, 78, 9),
                "ghz": wob(3.6, 4.6, 5), "gpu": wob(2, 55, 11, 1),
                "gpu_t": wob(40, 66, 13), "gpu_w": wob(12, 48, 8),
                "vram": None, "ram": (wob(9, 22, 17), 31.7),
                "ram_t": None, "disk_t": wob(38, 47, 21),
                "net": (wob(2e4, 3e6, 6), wob(1e4, 6e5, 7)),
                "lhm": "demo"}


# ------------------------------------------------------------------- UI ----
class Card:
    def __init__(self, args):
        self.args = args
        self.sensors = DemoSensors() if args.demo else WinSensors()
        self.claude_rows = []
        self.claude_err = None
        self.today = {"calls": 0, "out": 0}
        self.stop = threading.Event()

        self.root = tk.Tk()
        self.root.title("minimon")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if IS_WIN:
            self.root.attributes("-transparentcolor", TRANS)
        sans = "Segoe UI" if IS_WIN else "DejaVu Sans"
        mono = "Consolas" if IS_WIN else "DejaVu Sans Mono"
        self.sans = tkfont.Font(family=sans, size=9, weight="bold")
        self.tiny = tkfont.Font(family=sans, size=7, weight="bold")
        self.small = tkfont.Font(family=sans, size=7)
        self.mono = tkfont.Font(family=mono, size=8)
        self.mono_big = tkfont.Font(family=mono, size=10, weight="bold")
        self.lh = self.sans.metrics("linespace")
        self.mh = self.mono.metrics("linespace")
        self.th = self.tiny.metrics("linespace")
        self.val_w = self.mono_big.measure("100%") + 4
        self.row_h = self.lh + 4 + BARH + 10
        self.W = max(
            W,
            self.mono.measure("today  9.9K calls · 9.9M tok") + 2 * PAD + 4,
            self.sans.measure("Week·Fable") + 12
            + self.mono.measure("Wed 00:00") + self.val_w + 2 * PAD,
            self.sans.measure("RAM") + 12
            + self.mono.measure("88°C · 88.8/88 GB") + self.val_w + 2 * PAD)
        self.canvas = tk.Canvas(self.root, width=self.W, height=200,
                                bg=TRANS, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonPress-3>", self._menu)
        self.root.bind("<Escape>", lambda *_: self.quit())
        self.root.protocol("WM_DELETE_WINDOW", self.quit)


        self._place()
        self._last = time.monotonic()
        threading.Thread(target=self._claude_worker, daemon=True).start()
        threading.Thread(target=self._lhm_worker, daemon=True).start()
        self.tick()

    # --- window plumbing ---
    def _place(self):
        try:
            cfg = json.load(open(CONFIG))
            self.root.geometry("+%d+%d" % (cfg["x"], cfg["y"]))
        except (OSError, ValueError, KeyError):
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            self.root.geometry("+%d+%d" % (sw - self.W - 24, 24))

    def _drag_start(self, ev):
        self._dx, self._dy = ev.x, ev.y

    def _drag_move(self, ev):
        self.root.geometry("+%d+%d" % (ev.x_root - self._dx,
                                       ev.y_root - self._dy))

    def _menu(self, ev):
        m = tk.Menu(self.root, tearoff=0, bg="#1b1d23", fg="#e8e8ea",
                    activebackground="#7c5cff")
        m.add_command(label="Snap top-right", command=self._snap_tr)
        m.add_separator()
        m.add_command(label="Quit", command=self.quit)
        m.tk_popup(ev.x_root, ev.y_root)

    def _snap_tr(self):
        sw = self.root.winfo_screenwidth()
        self.root.geometry("+%d+%d" % (sw - self.W - 24, 24))

    def quit(self):
        self.stop.set()
        try:
            os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
            json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()},
                      open(CONFIG, "w"))
        except OSError:
            pass
        self.root.destroy()

    # --- background feeds ---
    def _lhm_worker(self):
        while not self.stop.is_set():
            self.sensors.refresh_lhm()
            self.stop.wait(2.0)

    def _claude_worker(self):
        stats = core.ClaudeStats()
        next_api = 0.0
        while not self.stop.is_set():
            try:
                self.today = stats.scan()
            except Exception:
                pass
            if time.time() >= next_api:
                api, err = core.fetch_claude_usage()
                if api:
                    self.claude_rows = core.usage_rows(api)
                    self.claude_err = None
                else:
                    self.claude_err = err
                next_api = time.time() + (
                    30 if (err and not self.claude_rows)
                    else (300 if err else 180))
            self.stop.wait(60)

    # --- drawing ---
    def _rround(self, x0, y0, x1, y1, r, **kw):
        c = self.canvas
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r,
               x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r,
               x0, y0 + r, x0, y0]
        return c.create_polygon(pts, smooth=True, **kw)

    def _bar(self, y, frac, color):
        x0, x1 = PAD, self.W - PAD
        self._rround(x0, y, x1, y + BARH, 3, fill=TRACK, outline="")
        fx = x0 + max(4, (x1 - x0) * min(1.0, max(0.0, frac)))
        self._rround(x0, y, fx, y + BARH, 3, fill=color, outline="")

    def _meter(self, y, name, sub, heat, value, frac, color):
        c = self.canvas
        c.create_text(PAD, y, text=name, anchor="nw", fill=FG, font=self.sans)
        sub_fill = HOT if (heat or 0) >= 85 else WARM if (heat or 0) >= 70 else DIM
        c.create_text(self.W - PAD - self.val_w - 6, y + (self.lh - self.mh),
                      text=sub, anchor="ne", fill=sub_fill, font=self.mono)
        c.create_text(self.W - PAD, y, text=value, anchor="ne", fill=FG,
                      font=self.mono_big)
        self._bar(y + self.lh + 4, frac, color)
        return y + self.row_h

    def tick(self):
        if self.stop.is_set():
            return
        now = time.monotonic()
        dt, self._last = now - self._last, now
        d = self.sensors.read_all(max(dt, 0.05))

        rows = list(self.claude_rows)
        c = self.canvas
        c.delete("all")

        y = 12
        c.create_text(PAD, y, text="MINIMON", anchor="nw", fill=DIM,
                      font=self.tiny)
        c.create_text(self.W - PAD, y - 2, text="✕", anchor="ne", fill=DIM,
                      font=self.sans, tags="close")
        c.tag_bind("close", "<Button-1>", lambda *_: self.quit())
        y += self.th + 8

        used, total = d["ram"]
        y = self._meter(y, "CPU", "%.0f°C" % d["cpu_t"] if d["cpu_t"] else "",
                        d["cpu_t"], "%3.0f%%" % d["cpu"], d["cpu"] / 100,
                        COLORS["cpu"])
        y = self._meter(y, "GPU", "%.0f°C" % d["gpu_t"] if d["gpu_t"] else "",
                        d["gpu_t"], "%3.0f%%" % d["gpu"], d["gpu"] / 100,
                        COLORS["gpu"])
        ram_sub = "%.1f/%.0f GB" % (used, total)
        if d.get("ram_t"):
            ram_sub = "%.0f°C · %s" % (d["ram_t"], ram_sub)
        y = self._meter(y, "RAM", ram_sub, d.get("ram_t"),
                        "%3.0f%%" % (100 * used / total),
                        used / total, COLORS["ram"])

        foot = []
        if d.get("ghz"):
            foot.append("%.1f GHz" % d["ghz"])
        if d.get("gpu_w"):
            foot.append("%.0f W" % d["gpu_w"])
        if d.get("disk_t"):
            foot.append("SSD %.0f°C" % d["disk_t"])
        c.create_text(PAD, y, text="  ·  ".join(foot), anchor="nw",
                      fill=DIM, font=self.mono)
        y += self.mh + 3
        c.create_text(PAD, y, anchor="nw", fill=DIM, font=self.mono,
                      text="↓ %s/s  ↑ %s/s" % (core.rate(d["net"][0]).strip(),
                                               core.rate(d["net"][1]).strip()))
        y += self.mh + 8

        c.create_line(PAD, y, self.W - PAD, y, fill="#2c2d36")
        y += 8
        c.create_text(PAD, y, text="CLAUDE CODE", anchor="nw", fill=DIM,
                      font=self.tiny)
        status = self.claude_err or ("no LHM" if d.get("lhm") == "none"
                                     and not d["cpu_t"] else "")
        c.create_text(self.W - PAD, y, text=status, anchor="ne", fill=DIM,
                      font=self.small)
        y += self.th + 8
        if rows:
            fills = [COLORS["u0"], COLORS["u1"], COLORS["u2"]]
            for i, (_k, label, pct, resets) in enumerate(rows):
                y = self._meter(y, label, short_reset(resets), pct,
                                "%3.0f%%" % pct, pct / 100,
                                fills[min(i, 2)])
        t = self.today
        c.create_text(PAD, y, anchor="nw", fill=DIM, font=self.mono,
                      text="today  %s calls · %s tok" % (
                          core.fmt_count(t.get("calls", 0)),
                          core.fmt_count(t.get("out", 0))))
        y += self.mh + 10

        c.config(height=y)
        bg = self._rround(1, 1, self.W - 1, y - 1, 14, fill=BG,
                          outline="#2c2d36")
        c.tag_lower(bg)

        self.root.after(int(self.args.interval * 1000), self.tick)

    def run(self):
        self.root.mainloop()


def main():
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except OSError:
            pass
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                    "minimon-win-single")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return 0
        globals()["_mutex"] = mutex

    p = argparse.ArgumentParser(description="minimon for Windows")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--demo", action="store_true",
                   help="synthetic sensor data (any OS)")
    args = p.parse_args()
    Card(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
