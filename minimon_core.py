"""minimon_core - OS-neutral helpers shared by all minimon front-ends
(Linux GTK card, GNOME daemon, Windows tkinter card).
"""


def rate(b):
    for unit, div in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if b >= div:
            return "%5.1f%s" % (b / div, unit)
    return "%5.0fB" % b
