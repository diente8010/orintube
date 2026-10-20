"""Turns on Xlib's thread support. Must be imported before GTK, GDK or GStreamer open the X display.

Why: nv3dsink draws into the app's X window from a GStreamer streaming thread while GTK
uses the same X connection from the main thread. Without XInitThreads() the two collide.
2026-10-19 test (embed_test.py, Big Buck Bunny 1080p60 in a Gtk.DrawingArea): the app
dumped core right after the pipeline started.

Does nothing on a system without libX11 (for example a Wayland-only machine).
"""

import ctypes
import ctypes.util


def enable_x11_threads():
    """Call XInitThreads() once. Returns True when it ran, False when libX11 is missing."""
    library_path = ctypes.util.find_library("X11")
    if library_path is None:
        return False
    ctypes.CDLL(library_path).XInitThreads()
    return True


enable_x11_threads()
