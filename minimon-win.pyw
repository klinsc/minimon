#!/usr/bin/env python3
"""minimon for Windows 11 - NZXT CAM "mini mode" style floating monitor.

Pure stdlib (tkinter + ctypes). CPU load, RAM and network come from the
Win32 API. Temperatures, GPU load/power, VRAM and fan-adjacent extras come
from LibreHardwareMonitor when it is running (Options -> Remote Web Server,
default port 8085; falls back to LHM's WMI namespace). Claude Code usage
comes from minimon_core, reading the same files the CLI uses.

It lives in the taskbar next to the Wi-Fi, volume and battery icons: a live
readout like the GNOME top-bar label (C4% 55° · G2% 45° · M48% · S9% W37%)
sits just left of the notification icons, and the tray icon itself is a
miniature of the card (CPU, GPU, RAM and Claude-session meters) with the
numbers in its tooltip. Left-click either to toggle the floating card,
right-click for the menu. All of it is plain Win32 through ctypes - still
no third-party packages.

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

    def _temp(self, *needles, reject=()):
        """Pick a temperature. Both LHM modes spell the kind into the key -
        "... / Temperatures / CPU Package" over HTTP, "CPU Package
        [Temperature]" over WMI - so requiring it keeps clocks, wattages and
        voltages out of a reading that is about to be printed with a degree
        sign."""
        return self._lhm_pick("temperature", *needles,
                              reject=("distance", "tjmax") + tuple(reject))

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
        """Poll whichever source works, re-probing both once a minute while
        neither does. A source that answers with no sensors counts as a
        failure: the WMI probe exits cleanly with an empty list when LHM is
        simply not running, and treating that as success would pin the mode
        to WMI and never look at the web server again - which is what
        happened when LibreHardwareMonitor was started after minimon."""
        if self._lhm_mode == "none" and time.time() - self._lhm_ts < 60:
            return
        for mode, fn in (("http", self._lhm_http), ("wmi", self._lhm_wmi)):
            if self._lhm_mode not in (None, "none", mode):
                continue
            try:
                sensors = fn()
            except Exception:
                continue
            if not sensors:
                continue
            self._lhm = sensors
            self._lhm_mode = mode
            self._lhm_ts = time.time()
            return
        self._lhm_mode = "none"
        self._lhm_ts = time.time()
        self._lhm = {}

    # --- combined snapshot ---
    def read_all(self, dt):
        used, total = self.mem()
        rx, tx = self.net(dt)
        return {
            "cpu": self.cpu_percent(),
            # Tctl on AMD, CPU Package on Intel, hottest core as a last resort
            # (Intel calls the node "13th Gen Intel Core ...", so "core"
            # matches the CPU's own name as well as its per-core sensors).
            "cpu_t": self._temp("cpu", "tctl") or self._temp("cpu", "package")
            or self._temp("cpu") or self._temp("core", reject=("gpu",)),
            "ghz": (self._lhm_pick("clock", "cpu", reject=("bus",)) or
                    self._lhm_pick("clock", "core",
                                   reject=("bus", "gpu", "memory")) or 0)
            / 1000 or None,
            "gpu": self._lhm_pick("load", "gpu", "core")
            or self._lhm_pick("", "gpu", "d3d", "3d") or 0.0,
            "gpu_t": self._temp("gpu", "core")
            or self._temp("gpu", reject=("hot spot", "junction", "memory")),
            "gpu_w": self._lhm_pick("power", "gpu"),
            "vram": None,
            "ram": (used, total),
            # the DIMM sensor, not the GPU's memory junction
            "ram_t": self._temp("memory", reject=("gpu", "junction", "vram")),
            "disk_t": self._temp("composite") or self._temp(
                reject=("cpu", "core", "gpu", "memory", "warning", "critical",
                        "battery", "ambient", "system", "chipset")),
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

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte),
                    ("AlphaFormat", ctypes.c_ubyte)]

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
    # taskbar readout
    _proto(_user32.FindWindowExW, wintypes.HWND, wintypes.HWND, wintypes.HWND,
           wintypes.LPCWSTR, wintypes.LPCWSTR)
    _proto(_user32.GetWindowRect, wintypes.BOOL, wintypes.HWND,
           ctypes.POINTER(wintypes.RECT))
    _proto(_user32.ScreenToClient, wintypes.BOOL, wintypes.HWND,
           ctypes.POINTER(wintypes.POINT))
    _proto(_user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
           wintypes.UINT)
    _proto(_user32.IsWindow, wintypes.BOOL, wintypes.HWND)
    _proto(_user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
    _proto(_user32.GetWindow, wintypes.HWND, wintypes.HWND, wintypes.UINT)
    _proto(_user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
    _proto(_user32.LoadCursorW, wintypes.HANDLE, wintypes.HINSTANCE,
           wintypes.LPVOID)
    _proto(_user32.DrawTextW, ctypes.c_int, wintypes.HDC, wintypes.LPCWSTR,
           ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT)
    _proto(_user32.UpdateLayeredWindow, wintypes.BOOL, wintypes.HWND,
           wintypes.HDC, ctypes.POINTER(wintypes.POINT),
           ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
           ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
           ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
    try:
        _proto(_user32.GetDpiForWindow, wintypes.UINT, wintypes.HWND)
    except AttributeError:          # before Windows 10 1607
        pass
    _proto(_gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
    _proto(_gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
    _proto(_gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
    _proto(_gdi32.CreateFontW, wintypes.HFONT, ctypes.c_int, ctypes.c_int,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
           wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
           wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR)
    _proto(_gdi32.SetBkMode, ctypes.c_int, wintypes.HDC, ctypes.c_int)
    _proto(_gdi32.SetTextColor, wintypes.COLORREF, wintypes.HDC,
           wintypes.COLORREF)
    _proto(_gdi32.GetTextExtentPoint32W, wintypes.BOOL, wintypes.HDC,
           wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.SIZE))

    WM_NULL, WM_CLOSE, WM_CONTEXTMENU, WM_APP = 0x0000, 0x0010, 0x007B, 0x8000
    WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0203
    NIN_SELECT, NIN_KEYSELECT = 0x0400, 0x0401
    NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x80
    NOTIFYICON_VERSION_4 = 4
    SM_CXSMICON = 49
    WM_MOUSEACTIVATE, WM_RBUTTONUP, MA_NOACTIVATE = 0x0021, 0x0205, 3
    WS_CHILD, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x40000000, 0x80000, 0x8000000
    SWP_NOACTIVATE, SWP_SHOWWINDOW, GW_HWNDPREV = 0x0010, 0x0040, 3
    DT_LABEL = 0x0001 | 0x0004 | 0x0020 | 0x0100 | 0x0800   # centred single line
    ULW_ALPHA, AC_SRC_ALPHA, ANTIALIASED_QUALITY, TRANSPARENT = 2, 1, 4, 1

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
        thread state is NULL". The WNDPROC therefore only queues (kind, arg)
        events through `post`, and Card._poll runs them on the Tk side.
        """
        CLASS = "minimon-tray"
        UID = 0x6D69                     # stable icon id ("mi")
        MSG_NOTIFY = WM_APP + 1          # icon events arrive here
        MSG_SHOW = WM_APP + 2            # a second launch posts this to us
        SETTINGS = r"Control Panel\NotifyIconSettings"   # Win11 per-icon prefs

        def __init__(self, post):
            self.post = post
            # Everything _wndproc may touch is set before CreateWindowExW,
            # which already delivers WM_NCCREATE and friends to it.
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
                    self.post(("show", None))
                    return 0
                if msg == self._taskbar_created:     # explorer restarted
                    self._added, self._retry_at = False, 0.0
                    self._add()
                    return 0
                if msg == WM_CLOSE:                  # e.g. a polite taskkill
                    self.post(("quit", None))
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
                    self.post(("toggle", None))
            elif ev == WM_CONTEXTMENU:   # right-click or Shift+F10 on the icon
                x = ctypes.c_short(wparam & 0xFFFF).value
                y = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                self.post(("menu", (x, y)))

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

    class TaskbarLabel:
        """The Linux top-bar readout, on Windows: a per-pixel-alpha layered
        child window parked inside explorer's Shell_TrayWnd just left of the
        notification icons (the TrafficMonitor technique; the taskbar has no
        text API of its own). The text is GDI grayscale-antialiased white on
        black and that coverage becomes the alpha channel, so the glyphs sit
        on the translucent taskbar with no box behind them. Newlines in the
        text stack lines, centred like the clock next door.

        Explorer keeps no record of the window: it is re-created whenever the
        taskbar is, and re-anchored on every update. Parenting across
        processes ties our input queue to explorer's, so the Tk thread must
        stay responsive (it does - about a millisecond of work per tick).
        Its WNDPROC only posts events, like TrayIcon's.
        """
        CLASS = "minimon-label"
        HIT = bytes([1]) + bytes(range(1, 256))   # alpha floor: 0 -> 1
        FONT = "Consolas"                # the Linux label is monospace too
        PT = 12                          # px at 96 dpi, the taskbar clock's size
        PAD = 6

        def __init__(self, post, side="right"):
            self.post = post
            self.side = side             # "right": beside the tray; "left": far end
            self.hwnd = self.parent = None
            self._text = None
            self._size = (0, 0)
            self._last_click = 0.0
            self._retry_at = 0.0
            self._tables = {}
            self._proc = WNDPROC(self._wndproc)      # must outlive the window
            self._hinst = _kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=self._hinst,
                           hCursor=_user32.LoadCursorW(None, 32512),  # IDC_ARROW
                           lpszClassName=self.CLASS)
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())

        # --- lifecycle ---
        def _attach(self):
            """Make sure our window exists inside the current taskbar."""
            if (self.hwnd and _user32.IsWindow(self.hwnd)
                    and _user32.IsWindow(self.parent)
                    and _user32.FindWindowW("Shell_TrayWnd", None) == self.parent):
                return True
            if self.hwnd:
                _user32.DestroyWindow(self.hwnd)     # usually died with explorer
                self.hwnd = self.parent = None
            now = time.monotonic()
            if now < self._retry_at:
                return False
            self._retry_at = now + 3.0
            tb = _user32.FindWindowW("Shell_TrayWnd", None)
            if not tb:
                return False
            rc = wintypes.RECT()
            _user32.GetWindowRect(tb, ctypes.byref(rc))
            if rc.bottom - rc.top > rc.right - rc.left:
                return False                         # vertical taskbar: no room
            hwnd = _user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_NOACTIVATE, self.CLASS, "minimon",
                WS_CHILD, 0, 0, 0, 0, tb, None, self._hinst, None)
            if not hwnd:            # e.g. DPI-context mismatch on an old Windows
                return False
            self.hwnd, self.parent = hwnd, tb
            self._text = None                        # repaint into the new window
            return True

        def update(self, text):
            if not self._attach():
                return
            if text != self._text and self._paint(text):
                self._text = text
            if self._text is not None:
                self._place()

        def remove(self):
            if self.hwnd and _user32.IsWindow(self.hwnd):
                _user32.DestroyWindow(self.hwnd)
            self.hwnd = self.parent = None

        # --- drawing ---
        @staticmethod
        def _color():
            """Taskbar text colour: near-black on a light taskbar, else white."""
            try:
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                    light = winreg.QueryValueEx(k, "SystemUsesLightTheme")[0]
            except OSError:
                light = 0
            return (0x10, 0x10, 0x10) if light else (0xff, 0xff, 0xff)

        def _table(self, c):
            """coverage -> premultiplied channel value, as a bytes.translate map"""
            t = self._tables.get(c)
            if t is None:
                t = self._tables[c] = bytes(v * c // 255 for v in range(256))
            return t

        def _paint(self, text):
            try:
                dpi = _user32.GetDpiForWindow(self.parent) or 96
            except AttributeError:
                dpi = 96
            px = max(11, round(self.PT * dpi / 96))
            pad = round(self.PAD * dpi / 96)
            rc = wintypes.RECT()
            _user32.GetWindowRect(self.parent, ctypes.byref(rc))
            h = rc.bottom - rc.top
            hdc = _gdi32.CreateCompatibleDC(None)
            font = _gdi32.CreateFontW(-px, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0,
                                      ANTIALIASED_QUALITY, 0, self.FONT)
            old_font = _gdi32.SelectObject(hdc, font)
            _gdi32.SetBkMode(hdc, TRANSPARENT)
            _gdi32.SetTextColor(hdc, 0xFFFFFF)
            lines = text.split("\n")
            ext, widths, cy = wintypes.SIZE(), [], 0
            for ln in lines:
                _gdi32.GetTextExtentPoint32W(hdc, ln, len(ln), ctypes.byref(ext))
                widths.append(ext.cx)
                cy = max(cy, ext.cy)
            gap = round(dpi / 96)
            w = max(widths) + 2 * pad
            y = (h - (len(lines) * cy + (len(lines) - 1) * gap)) // 2
            bih = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER),
                                   biWidth=w, biHeight=-h, biPlanes=1,
                                   biBitCount=32)
            bits = ctypes.c_void_p()
            dib = _gdi32.CreateDIBSection(hdc, ctypes.byref(bih), 0,
                                          ctypes.byref(bits), None, 0)
            ok = False
            if dib:
                old_bmp = _gdi32.SelectObject(hdc, dib)
                n = w * h * 4
                ctypes.memset(bits, 0, n)
                for i, ln in enumerate(lines):
                    top = y + i * (cy + gap)
                    box = wintypes.RECT(0, top, w, top + cy)
                    _user32.DrawTextW(hdc, ln, -1, ctypes.byref(box), DT_LABEL)
                # white-on-black coverage -> premultiplied BGRA in the text colour
                cov = ctypes.string_at(bits, n)[0::4]
                r, g, b = self._color()
                out = bytearray(n)
                out[0::4] = cov.translate(self._table(b))
                out[1::4] = cov.translate(self._table(g))
                out[2::4] = cov.translate(self._table(r))
                # Layered windows hit-test per pixel and alpha 0 lets a click
                # fall through to the taskbar, so keep every pixel at >= 1.
                out[3::4] = cov.translate(self.HIT)
                ctypes.memmove(bits, bytes(out), n)
                blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
                ok = _user32.UpdateLayeredWindow(
                    self.hwnd, None, None, ctypes.byref(wintypes.SIZE(w, h)),
                    hdc, ctypes.byref(wintypes.POINT(0, 0)), 0,
                    ctypes.byref(blend), ULW_ALPHA)
                _gdi32.SelectObject(hdc, old_bmp)
                _gdi32.DeleteObject(dib)
            _gdi32.SelectObject(hdc, old_font)
            _gdi32.DeleteObject(font)
            _gdi32.DeleteDC(hdc)
            if ok:
                self._size = (w, h)
            return bool(ok)

        @staticmethod
        def _widgets_button():
            """Windows 11 parks its Widgets button at the taskbar's left end."""
            try:
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced") as k:
                    return bool(winreg.QueryValueEx(k, "TaskbarDa")[0])
            except OSError:
                return False

        def _place(self):
            """Anchor to the left edge of the notification icons (or, with
            side="left", to the taskbar's left end), and stay on top."""
            prc, trc = wintypes.RECT(), wintypes.RECT()
            _user32.GetWindowRect(self.parent, ctypes.byref(prc))
            w, h = self._size
            if self.side == "left":
                x0 = prc.left + (2 * h if self._widgets_button() else 0)
            else:
                tray = _user32.FindWindowExW(self.parent, None, "TrayNotifyWnd", None)
                x0 = (trc.left if tray and _user32.GetWindowRect(tray, ctypes.byref(trc))
                      else prc.right) - w
            mine = wintypes.RECT()
            _user32.GetWindowRect(self.hwnd, ctypes.byref(mine))
            if (_user32.IsWindowVisible(self.hwnd)
                    and not _user32.GetWindow(self.hwnd, GW_HWNDPREV)
                    and (mine.left, mine.top, mine.right, mine.bottom)
                    == (x0, prc.top, x0 + w, prc.top + h)):
                return
            pt = wintypes.POINT(x0, prc.top)
            _user32.ScreenToClient(self.parent, ctypes.byref(pt))
            _user32.SetWindowPos(self.hwnd, None, pt.x, pt.y, w, h,
                                 SWP_NOACTIVATE | SWP_SHOWWINDOW)

        # --- window side (no Tk calls in here) ---
        def _wndproc(self, hwnd, msg, wparam, lparam):
            try:
                if msg == WM_LBUTTONUP:
                    now = time.monotonic()
                    if now - self._last_click > 0.3:   # a double-click's 2nd up
                        self._last_click = now
                        self.post(("toggle", None))
                    return 0
                if msg == WM_RBUTTONUP:
                    pt = wintypes.POINT()
                    _user32.GetCursorPos(ctypes.byref(pt))
                    self.post(("menu", (pt.x, pt.y)))
                    return 0
                if msg == WM_MOUSEACTIVATE:
                    return MA_NOACTIVATE
            except Exception:
                if sys.stderr:
                    traceback.print_exc()
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


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
        self._events = collections.deque()   # from the tray/label window procs
        self.label = None
        if IS_WIN and not args.no_tray:
            try:
                self.tray = TrayIcon(post=self._events.append)
            except OSError:
                self.tray = None
        if self.tray:
            if not args.no_label:
                try:
                    self.label = TaskbarLabel(post=self._events.append,
                                              side=args.label_side)
                except OSError:
                    self.label = None
            if args.hidden:
                self.hide()
            self.root.after(2000, self._pin_once)
            self.root.after(40, self._poll)

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

    def _poll(self):
        """Tk timer: run the events the tray icon and the taskbar readout
        queued from their window procs. Not re-entrant - while a popup menu
        is open the next poll simply is not scheduled yet."""
        while self._events:
            kind, arg = self._events.popleft()
            if kind == "toggle":
                self.toggle()
            elif kind == "show":
                self.show()
            elif kind == "menu":
                anchor = self.tray.hwnd if self.tray else None
                if anchor:
                    _user32.SetForegroundWindow(anchor)  # closes on outside click
                self._build_menu().tk_popup(*arg)
                if anchor and self.tray and self.tray.hwnd:
                    _user32.PostMessageW(anchor, WM_NULL, 0, 0)
            elif kind == "quit":
                self.quit()
        if not self.stop.is_set():
            self.root.after(40, self._poll)

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
        if self.label:
            self.label.remove()
            self.label = None
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

    # --- taskbar readout ---
    @staticmethod
    def _quota_tags(rows):
        """S  9% W 37% F 38% - the GNOME extension's one-letter Claude summary,
        each percent right-aligned in 3 so the readout keeps a constant width."""
        out = []
        for key, _label, pct, _r in rows:
            if key.startswith("session") or key == "five_hour":
                tag = "S"
            elif key in ("weekly_all", "seven_day"):
                tag = "W"
            elif ":" in key and key.startswith("weekly"):
                tag = key.split(":", 1)[1][:1].upper()
            elif key.startswith("seven_day_"):
                tag = key[10:11].upper()
            else:
                continue
            out.append("%s%3.0f%%" % (tag, pct))
        return " ".join(out) or "CC --"

    def _label_text(self, d, rows):
        """The GNOME top-bar label, stacked as two lines (hardware over
        Claude) because the taskbar is twice as tall as the GNOME bar and
        its clock is two lines too; one --format template per line.

        Every number is right-aligned in a fixed field - percentages in 3
        (they reach 100), temperatures in 2 - so that in the monospace font
        the text keeps a constant width and the readout does not jitter left
        and right as values cross between one, two and three digits."""
        used, total = d["ram"]
        deg = lambda t: "%2.0f" % t if t else "--"
        if self.args.format:
            s = self._session_pct(rows)
            values = dict(
                cpu="%3.0f" % d["cpu"], ct=deg(d["cpu_t"]),
                gpu="%3.0f" % d["gpu"], gt=deg(d["gpu_t"]),
                mem="%3.0f" % (100 * used / total), mt=deg(d.get("ram_t")),
                cc="--" if s is None else "%3.0f" % s,
                claude=self._quota_tags(rows))
            lines = []
            for fmt in self.args.format:
                try:
                    lines.append(fmt.format(**values))
                except (KeyError, IndexError, ValueError):
                    lines.append(fmt)          # show the bad template as-is
            return "\n".join(lines)
        return "C%3.0f%% %s° · G%3.0f%%%s · M%3.0f%%%s\n%s" % (
            d["cpu"], deg(d["cpu_t"]), d["gpu"],
            " %2.0f°" % d["gpu_t"] if d["gpu_t"] else "",
            100 * used / total,
            " %2.0f°" % d["ram_t"] if d.get("ram_t") else "",
            self._quota_tags(rows))

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
        if self.label:
            self.label.update(self._label_text(d, rows))
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
                   help="start with the card hidden (taskbar only)")
    p.add_argument("--no-label", action="store_true",
                   help="no live readout in the taskbar, just the tray icon")
    p.add_argument("--label-side", choices=("right", "left"), default="right",
                   help="where the readout sits: right = beside the tray icons "
                        "(default), left = the taskbar's left end, for taskbars "
                        "too crowded on the right")
    p.add_argument("--format", nargs="+", default=None, metavar="LINE",
                   help="taskbar readout, one template per line, with {cpu} "
                        "{ct} {gpu} {gt} {mem} {mt} {cc} {claude} tokens "
                        "(default: the GNOME top-bar label as two lines, "
                        "C4%% 55° · G2%% 45° · M48%% over S9%% W37%% F38%%)")
    args = p.parse_args()

    if IS_WIN:
        # Per-monitor v2 is the taskbar's own DPI context, and a window can
        # only be parented into explorer's taskbar when both sides match.
        try:
            _user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_ssize_t,)
            if not _user32.SetProcessDpiAwarenessContext(-4):
                raise OSError
        except (AttributeError, OSError):
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
