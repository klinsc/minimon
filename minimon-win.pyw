#!/usr/bin/env python3
"""minimon for Windows 11 - NZXT CAM "mini mode" style floating monitor.

Pure stdlib (tkinter + ctypes). CPU load, RAM and network come from the
Win32 API. Temperatures, GPU load/power, VRAM and fan-adjacent extras come
from LibreHardwareMonitor when it is running (Options -> Remote Web Server,
default port 8085; falls back to LHM's WMI namespace). Claude Code usage
comes from minimon_core, reading the same files the CLI uses.

It lives in the taskbar's notification area next to the Wi-Fi, volume and
battery icons: the tray icon is a live miniature of the card (CPU, GPU, RAM
and Claude-session meters), hovering it shows the numbers, a left-click
toggles the floating card and a right-click opens the menu. That is plain
Shell_NotifyIcon through ctypes - still no third-party packages.

Run with pythonw minimon-win.pyw (or the packaged minimon-win-x64.exe).
--demo renders with synthetic sensor data on any OS.
"""
import argparse
import collections
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
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


# -------------------------------------------------------------- tray icon --
def _rgb(hexcolor):
    return tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))


def tray_pixels(size, bars):
    """Draw the tray glyph: the card in miniature - a dark rounded tile with
    one horizontal meter per (fraction, "#rrggbb") in `bars`.

    Returns top-down BGRA bytes for a size x size 32-bit DIB. Alpha is
    straight (not premultiplied), which is what CreateIconIndirect expects;
    only the round corners are partially transparent."""
    s = size
    buf = bytearray(s * s * 4)

    def put(x, y, rgb, a=255):
        i = (y * s + x) * 4
        buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = rgb[2], rgb[1], rgb[0], a

    r = max(2, round(s / 4))
    bg = _rgb(BG)
    for y in range(s):
        for x in range(s):
            # corner pixels get 4x4-supersampled coverage of the round corner
            cx = r if x < r else s - r if x >= s - r else None
            cy = r if y < r else s - r if y >= s - r else None
            if cx is None or cy is None:
                put(x, y, bg)
                continue
            inside = 0
            for j in range(4):
                for i in range(4):
                    dx = x + (i + 0.5) / 4 - cx
                    dy = y + (j + 0.5) / 4 - cy
                    if dx * dx + dy * dy <= r * r:
                        inside += 1
            if inside:
                put(x, y, bg, inside * 255 // 16)

    n = len(bars)
    gap = max(1, s // 16)
    margin = max(2, s // 8)
    h = max(1, (s - 2 * margin - (n - 1) * gap) // n)
    top = (s - (n * h + (n - 1) * gap)) // 2
    x0, x1 = margin, s - margin
    track = _rgb(TRACK)
    for i, (frac, color) in enumerate(bars):
        fill = 0 if frac <= 0 else max(1, round((x1 - x0) * min(1.0, frac)))
        rgb = _rgb(color)
        y0 = top + i * (h + gap)
        for y in range(y0, y0 + h):
            for x in range(x0, x1):
                put(x, y, rgb if x < x0 + fill else track)
    return buf


if IS_WIN:
    import winreg
    from ctypes import wintypes

    # Private DLL handles so our argtypes never leak into ctypes.windll users.
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):    # Vista+ layout (976 bytes x64)
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", ctypes.c_byte * 16),
                    ("hBalloonIcon", wintypes.HICON)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    def _proto(fn, restype, *argtypes):
        fn.restype, fn.argtypes = restype, argtypes

    _proto(_user32.DefWindowProcW, ctypes.c_ssize_t, wintypes.HWND,
           wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _proto(_user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
    _proto(_user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD,
           wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    _proto(_user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    _proto(_user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT,
           wintypes.WPARAM, wintypes.LPARAM)
    _proto(_user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
    _proto(_user32.FindWindowW, wintypes.HWND, wintypes.LPCWSTR,
           wintypes.LPCWSTR)
    _proto(_user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
    _proto(_user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    _proto(_user32.CreateIconIndirect, wintypes.HICON, ctypes.POINTER(ICONINFO))
    _proto(_user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)
    _proto(_gdi32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC,
           ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
           ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
    _proto(_gdi32.CreateBitmap, wintypes.HBITMAP, ctypes.c_int, ctypes.c_int,
           wintypes.UINT, wintypes.UINT, ctypes.c_void_p)
    _proto(_gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
    _proto(_shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD,
           ctypes.POINTER(NOTIFYICONDATAW))
    _proto(_kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)

    WM_NULL, WM_CLOSE, WM_CONTEXTMENU, WM_APP = 0x0000, 0x0010, 0x007B, 0x8000
    WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0203
    NIN_SELECT, NIN_KEYSELECT = 0x0400, 0x0401
    NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x80
    NOTIFYICON_VERSION_4 = 4
    SM_CXSMICON = 49

    def make_icon(size, pixels):
        """HICON (caller DestroyIcon()s it) from top-down BGRA bytes, or None."""
        bih = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER),
                               biWidth=size, biHeight=-size, biPlanes=1,
                               biBitCount=32)
        bits = ctypes.c_void_p()
        color = _gdi32.CreateDIBSection(None, ctypes.byref(bih), 0,
                                        ctypes.byref(bits), None, 0)
        if not color:
            return None
        ctypes.memmove(bits, bytes(pixels), len(pixels))
        stride = (size + 15) // 16 * 2          # 1-bpp rows are WORD aligned
        mask = bytearray(stride * size)         # bit set = transparent
        for y in range(size):
            for x in range(size):
                if not pixels[(y * size + x) * 4 + 3]:
                    mask[y * stride + x // 8] |= 0x80 >> (x % 8)
        hmask = _gdi32.CreateBitmap(size, size, 1, 1, bytes(mask))
        info = ICONINFO(True, 0, 0, hmask, color)
        icon = _user32.CreateIconIndirect(ctypes.byref(info))
        _gdi32.DeleteObject(hmask)
        _gdi32.DeleteObject(color)
        return icon

    class TrayIcon:
        """The notification-area icon plus the hidden window that owns it.

        The window lives on the tkinter thread, so Tk's event loop pumps its
        messages and nothing else is needed. The WNDPROC must not call into
        Tk though: a ctypes callback runs while _tkinter has the Tcl thread
        state parked, and a nested Tk call from there clears that state, so
        the next Tk->Python callback dies with "PyEval_RestoreThread ...
        thread state is NULL". The WNDPROC therefore only queues events and a
        Tk timer (_poll) runs them on the Tk side a few ms later.
        """
        CLASS = "minimon-tray"
        UID = 0x6D69                     # stable icon id ("mi")
        MSG_NOTIFY = WM_APP + 1          # icon events arrive here
        MSG_SHOW = WM_APP + 2            # a second launch posts this to us
        POLL_MS = 40
        SETTINGS = r"Control Panel\NotifyIconSettings"   # Win11 per-icon prefs

        def __init__(self, root, on_toggle, on_menu, on_show, on_quit):
            self.root = root
            self.on_toggle, self.on_menu = on_toggle, on_menu
            self.on_show, self.on_quit = on_show, on_quit
            # Everything _wndproc may touch is set before CreateWindowExW,
            # which already delivers WM_NCCREATE and friends to it.
            self._pending = collections.deque()
            self._icon = None
            self._added = False
            self._retry_at = 0.0
            self._last_toggle = 0.0
            self._dblclk_at = 0.0
            self._taskbar_created = _user32.RegisterWindowMessageW(
                "TaskbarCreated")
            self._proc = WNDPROC(self._wndproc)      # must outlive the window
            hinst = _kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst,
                           lpszClassName=self.CLASS)
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = _user32.CreateWindowExW(
                0, self.CLASS, "minimon", 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            try:    # let a medium-IL explorer reach us even when we run elevated
                _user32.ChangeWindowMessageFilterEx(
                    self.hwnd, self._taskbar_created, 1, None)
            except (AttributeError, OSError):
                pass
            self._nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW),
                                        hWnd=self.hwnd, uID=self.UID,
                                        uCallbackMessage=self.MSG_NOTIFY)
            root.after(self.POLL_MS, self._poll)

        # --- shell side ---
        def update(self, bars, tip):
            """Redraw the glyph from (fraction, color) bars and set the tooltip."""
            size = max(16, _user32.GetSystemMetrics(SM_CXSMICON))
            icon = make_icon(size, tray_pixels(size, bars))
            if not icon:
                return
            old, self._icon = self._icon, icon
            nid = self._nid
            nid.hIcon = icon
            if not self._added:
                self._add()
            if self._added:
                nid.uFlags = NIF_ICON | NIF_TIP | NIF_SHOWTIP
                nid.szTip = tip[:127]
                if not _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
                    self._added = False      # explorer went away; re-add later
            if old:
                _user32.DestroyIcon(old)     # the shell keeps its own copy

        def _add(self):
            now = time.monotonic()
            if now < self._retry_at:
                return
            self._retry_at = now + 5.0   # Shell_NotifyIcon blocks ~4 s if the shell hangs
            nid = self._nid
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
            nid.szTip = "minimon"        # what Settings > Taskbar lists us as
            if _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                nid.uVersion = NOTIFYICON_VERSION_4
                _shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
                self._added = True
            elif _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
                self._added = True       # still registered (spurious TaskbarCreated)

        def remove(self):
            if self._added:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                self._added = False
            if self._icon:
                _user32.DestroyIcon(self._icon)
                self._icon = None
            if self.hwnd:
                _user32.DestroyWindow(self.hwnd)
                self.hwnd = None

        # --- window side (no Tk calls in here, see the class docstring) ---
        def _wndproc(self, hwnd, msg, wparam, lparam):
            try:
                if msg == self.MSG_NOTIFY:
                    self._event(lparam & 0xFFFF, wparam)
                    return 0
                if msg == self.MSG_SHOW:
                    self._pending.append(("show", None))
                    return 0
                if msg == self._taskbar_created:     # explorer restarted
                    self._added, self._retry_at = False, 0.0
                    self._add()
                    return 0
                if msg == WM_CLOSE:                  # e.g. a polite taskkill
                    self._pending.append(("quit", None))
                    return 0
            except Exception:       # never unwind into the message loop
                if sys.stderr:      # pythonw has none; a console/log does
                    traceback.print_exc()
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _event(self, ev, wparam):
            now = time.monotonic()
            if ev == WM_LBUTTONDBLCLK:
                self._dblclk_at = now    # its first click already toggled
            elif ev in (WM_LBUTTONUP, NIN_SELECT, NIN_KEYSELECT):
                if ev == WM_LBUTTONUP and now - self._dblclk_at < 0.5:
                    return
                if now - self._last_toggle > 0.3:   # NIN_* echoes of one click
                    self._last_toggle = now
                    self._pending.append(("toggle", None))
            elif ev == WM_CONTEXTMENU:   # right-click or Shift+F10 on the icon
                x = ctypes.c_short(wparam & 0xFFFF).value
                y = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                self._pending.append(("menu", (x, y)))

        # --- Tk side ---
        def _poll(self):
            """Tk timer: run the queued icon events. Not re-entrant - while a
            popup menu is open the next poll simply is not scheduled yet."""
            while self._pending:
                kind, arg = self._pending.popleft()
                if kind == "toggle":
                    self.on_toggle()
                elif kind == "show":
                    self.on_show()
                elif kind == "menu":
                    _user32.SetForegroundWindow(self.hwnd)  # closes on outside click
                    self.on_menu(*arg)
                    if self.hwnd:
                        _user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
                elif kind == "quit":
                    self.on_quit()
            if self.hwnd:                # cleared by remove() on shutdown
                self.root.after(self.POLL_MS, self._poll)

        # --- Windows 11 "show in the taskbar corner" (vs. the ^ overflow) ---
        def _settings_key(self):
            """Explorer's per-icon settings key for us, once it exists (it is
            created shortly after the first NIM_ADD)."""
            exe = os.path.basename(sys.executable).lower()
            try:
                root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.SETTINGS)
            except OSError:
                return None
            with root:
                for i in range(4096):
                    try:
                        name = winreg.EnumKey(root, i)
                    except OSError:
                        return None
                    try:
                        with winreg.OpenKey(root, name) as k:
                            path = winreg.QueryValueEx(k, "ExecutablePath")[0]
                            uid = winreg.QueryValueEx(k, "UID")[0]
                    except OSError:
                        continue
                    if (uid == self.UID and
                            os.path.basename(str(path)).lower() == exe):
                        return self.SETTINGS + "\\" + name
            return None

        def pinned(self):
            """True/False = shown in the corner / hidden behind the overflow;
            None = the shell has no entry for us yet."""
            key = self._settings_key()
            if not key:
                return None
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                    return bool(winreg.QueryValueEx(k, "IsPromoted")[0])
            except OSError:
                return False

        def pin(self, show=True):
            """Move the icon into the always-visible corner (or back into the
            overflow). Explorer applies it live. False = no entry yet."""
            key = self._settings_key()
            if not key:
                return False
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "IsPromoted", 0, winreg.REG_DWORD,
                                      1 if show else 0)
                return True
            except OSError:
                return False


# ------------------------------------------------------------------- UI ----
class Card:
    def __init__(self, args):
        self.args = args
        self.sensors = DemoSensors() if args.demo else WinSensors()
        self.claude_rows = []
        self.claude_err = None
        self.today = {"calls": 0, "out": 0}
        self.stop = threading.Event()
        self.cfg = {}
        self.hidden = False
        self.tray = None

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
        self.canvas.bind("<ButtonRelease-1>", lambda *_: self._save_cfg())
        self.canvas.bind("<ButtonPress-3>", self._menu)
        self.root.bind("<Escape>", lambda *_: self.close())
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._place()
        if IS_WIN and not args.no_tray:
            try:
                self.tray = TrayIcon(self.root, on_toggle=self.toggle,
                                     on_menu=self._tray_menu, on_show=self.show,
                                     on_quit=self.quit)
            except OSError:
                self.tray = None
        if self.tray:
            if args.hidden:
                self.hide()
            self.root.after(2000, self._pin_once)

        self._last = time.monotonic()
        threading.Thread(target=self._claude_worker, daemon=True).start()
        threading.Thread(target=self._lhm_worker, daemon=True).start()
        self.tick()

    # --- window plumbing ---
    def _place(self):
        try:
            self.cfg = json.load(open(CONFIG))
            if not isinstance(self.cfg, dict):
                self.cfg = {}
        except (OSError, ValueError):
            self.cfg = {}
        try:
            self.pos = (int(self.cfg["x"]), int(self.cfg["y"]))
        except (KeyError, TypeError, ValueError):
            self.root.update_idletasks()
            self.pos = (self.root.winfo_screenwidth() - self.W - 24, 24)
        self.root.geometry("+%d+%d" % self.pos)

    def _save_cfg(self):
        self.cfg["x"], self.cfg["y"] = self.pos
        try:
            os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
            with open(CONFIG, "w") as fh:
                json.dump(self.cfg, fh)
        except OSError:
            pass

    def _drag_start(self, ev):
        self._dx, self._dy = ev.x, ev.y

    def _drag_move(self, ev):
        self.pos = (ev.x_root - self._dx, ev.y_root - self._dy)
        self.root.geometry("+%d+%d" % self.pos)

    def _snap_tr(self):
        self.pos = (self.root.winfo_screenwidth() - self.W - 24, 24)
        self.root.geometry("+%d+%d" % self.pos)
        self.show()
        self._save_cfg()

    def show(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.hidden = False

    def hide(self):
        self.root.withdraw()
        self.hidden = True

    def toggle(self):
        if self.hidden:
            self.show()
        else:
            self.hide()

    def close(self):
        """The card's close gesture: into the tray when we have one, else quit."""
        if self.tray:
            self.hide()
        else:
            self.quit()

    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0, bg="#1b1d23", fg="#e8e8ea",
                    activebackground="#7c5cff", selectcolor="#e8e8ea")
        if self.tray:
            m.add_command(label="Show card" if self.hidden else "Hide card",
                          command=self.toggle)
        m.add_command(label="Snap top-right", command=self._snap_tr)
        if self.tray:
            pinned = self.tray.pinned()
            if pinned is not None:
                self._pin_var = tk.BooleanVar(self.root, value=pinned)
                m.add_checkbutton(label="Always show icon in taskbar",
                                  variable=self._pin_var,
                                  command=lambda: self.tray.pin(self._pin_var.get()))
        m.add_separator()
        m.add_command(label="Quit", command=self.quit)
        return m

    def _menu(self, ev):
        self._build_menu().tk_popup(ev.x_root, ev.y_root)

    def _tray_menu(self, x, y):
        self._build_menu().tk_popup(x, y)

    def _pin_once(self, tries=0):
        """First run only: put the icon in the always-visible taskbar corner
        (Windows 11 hides new icons behind the ^ overflow by default). Noted
        per executable so a later choice in Settings or the menu sticks."""
        done = self.cfg.get("tray_pinned")
        if not isinstance(done, dict):
            done = self.cfg["tray_pinned"] = {}
        if done.get(sys.executable) or not self.tray:
            return
        if self.tray.pin(True):
            done[sys.executable] = True
            self._save_cfg()
        elif tries < 10:     # explorer writes its entry a moment after NIM_ADD
            self.root.after(3000, lambda: self._pin_once(tries + 1))

    def quit(self):
        if self.stop.is_set():
            return
        self.stop.set()
        self._save_cfg()
        if self.tray:
            self.tray.remove()
            self.tray = None
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

    # --- tray feed ---
    @staticmethod
    def _session_pct(rows):
        for key, _label, pct, _resets in rows:
            if key.startswith("session") or key == "five_hour":
                return pct
        return None

    def _tray_bars(self, d, rows):
        used, total = d["ram"]
        hot = lambda t: (t or 0) >= 85
        s = self._session_pct(rows) or 0
        return [(d["cpu"] / 100, HOT if hot(d["cpu_t"]) else COLORS["cpu"]),
                (d["gpu"] / 100, HOT if hot(d["gpu_t"]) else COLORS["gpu"]),
                (used / total, COLORS["ram"]),
                (s / 100, HOT if s >= 90 else COLORS["u0"])]

    def _tray_tip(self, d, rows):
        used, total = d["ram"]
        deg = lambda t: " %.0f°" % t if t else ""
        hw = "CPU %.0f%%%s · GPU %.0f%%%s · RAM %.0f%%" % (
            d["cpu"], deg(d["cpu_t"]), d["gpu"], deg(d["gpu_t"]),
            100 * used / total)
        if rows:
            claude = " · ".join("%s %.0f%%" % (label, pct)
                                for _k, label, pct, _r in rows)
        else:
            claude = "Claude: %s" % (self.claude_err or "…")
        t = self.today
        today = "today %s calls · %s tok" % (core.fmt_count(t.get("calls", 0)),
                                             core.fmt_count(t.get("out", 0)))
        lines = ["minimon", hw, claude, today]
        extra = sum(len(s) for s in lines) + 3 - 127   # szTip holds 127 chars
        if extra > 0:
            lines[2] = claude[:max(0, len(claude) - extra - 1)] + "…"
        return "\n".join(lines)[:127]

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
        if self.tray:
            self.tray.update(self._tray_bars(d, rows), self._tray_tip(d, rows))
        if not self.hidden:
            self._draw(d, rows)
        self.root.after(int(self.args.interval * 1000), self.tick)

    def _draw(self, d, rows):
        c = self.canvas
        c.delete("all")

        y = 12
        c.create_text(PAD, y, text="MINIMON", anchor="nw", fill=DIM,
                      font=self.tiny)
        c.create_text(self.W - PAD, y - 2, text="✕", anchor="ne", fill=DIM,
                      font=self.sans, tags="close")
        c.tag_bind("close", "<Button-1>", lambda *_: self.close())
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

    def run(self):
        self.root.mainloop()


def main():
    p = argparse.ArgumentParser(description="minimon for Windows")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--demo", action="store_true",
                   help="synthetic sensor data (any OS)")
    p.add_argument("--no-tray", action="store_true",
                   help="no notification-area icon; the card's close button quits")
    p.add_argument("--hidden", action="store_true",
                   help="start with the card hidden (tray icon only)")
    args = p.parse_args()

    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except OSError:
            pass
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                    "minimon-win-single")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            # Already running: ask that instance to pop its card back up.
            other = _user32.FindWindowW(TrayIcon.CLASS, None)
            if other:
                _user32.PostMessageW(other, TrayIcon.MSG_SHOW, 0, 0)
            return 0
        globals()["_mutex"] = mutex

    Card(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
