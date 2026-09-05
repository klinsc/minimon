"""minimon_core - OS-neutral Claude Code usage plumbing shared by all
minimon front-ends (Linux GTK card, GNOME daemon, Windows tkinter card).

Reads Claude Code's own credential file to query the official usage
endpoint, and tallies today's calls/tokens from the transcript JSONLs.
"""
import datetime
import glob
import json
import os
import time
import urllib.request

# Default to Claude Code's standard locations; override with env vars when a
# platform or install puts them elsewhere (some Windows setups do).
CRED_FILE = os.environ.get(
    "MINIMON_CRED_FILE", os.path.expanduser("~/.claude/.credentials.json"))
PROJECTS = os.environ.get(
    "MINIMON_PROJECTS", os.path.expanduser("~/.claude/projects"))
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
