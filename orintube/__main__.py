"""Starts OrinTube.

Run:
    orintube                       open on Home (your recommendations)
    orintube <youtube-link>        open and play that video straight away
    orintube --no-sign-in          do not read the browser's YouTube session (search only)
"""

import argparse
import os

from . import x11threads  # noqa: F401  (must come before GTK/GStreamer open the X display)

import gi  # noqa: E402
from gi.repository import GLib  # noqa: E402

# 2026-10-19 (user: "there isnt an icon either"): the window was named after the file
# (__main__.py in the top bar), so GNOME could not match it to orintube.desktop and showed
# no icon. The program name becomes the window class (WM_CLASS "orintube"), which the
# desktop file names in StartupWMClass. It must be set before GTK opens the display.
GLib.set_prgname("orintube")
GLib.set_application_name("OrinTube")

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .ui import ViewerWindow  # noqa: E402
from .youtube import YouTube  # noqa: E402


def parse_arguments():
    """Read the command line. Returns an argparse.Namespace with .url and .no_sign_in."""
    parser = argparse.ArgumentParser(prog="orintube", description="YouTube with the Jetson hardware decoder.")
    parser.add_argument("url", nargs="?", help="YouTube link to play right away (default: open Home)")
    parser.add_argument("--no-sign-in", action="store_true",
                        help="do not read the browser's YouTube session; only search works (default: use it)")
    return parser.parse_args()


def main():
    """Open the window and run GTK until it is closed."""
    arguments = parse_arguments()
    Gtk.Window.set_default_icon_name("orintube")
    window = ViewerWindow(YouTube(use_sign_in=not arguments.no_sign_in))
    window.start(arguments.url)
    Gtk.main()
    # 2026-10-19 review: worker threads (GStreamer control, thumbnail downloads) can still be
    # busy, and Python would wait for them at exit. The player has already stopped (or
    # given up after 3 s) in on_destroy, so leave right away.
    os._exit(0)


if __name__ == "__main__":
    main()
