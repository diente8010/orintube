"""Scripted on-screen run of the whole app. Needs the Jetson desktop, the network and the speakers.

What it does (about 40 s): opens the window, waits for Home, plays the first video,
seeks to 1:00, pauses and resumes, goes fullscreen and back, returns to the grid, then
closes. Screenshots go to SCREENSHOT_DIR, and every step is printed with the time and
playback position. It is not a unit test: a person (or Claude) reads the output and screenshots.

Run:  python3 ~/orintube/tests/smoke_ui.py [screenshot-dir]
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import orintube.x11threads  # noqa: E402,F401

from orintube.ui import ViewerWindow  # noqa: E402  (also selects GTK 3)
from gi.repository import GLib, Gtk  # noqa: E402
from orintube.youtube import YouTube  # noqa: E402

SCREENSHOT_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
START = time.monotonic()


def log(*parts):
    """Print a line prefixed with seconds since start."""
    print(f"{time.monotonic() - START:5.1f}s", *parts, flush=True)


def screenshot(name):
    """Save a screenshot as SCREENSHOT_DIR/<name>.png without blocking the GTK main loop."""
    subprocess.Popen(["gnome-screenshot", "-f", os.path.join(SCREENSHOT_DIR, f"{name}.png")])
    log("screenshot", name)


def main():
    """Run the scripted steps on a real ViewerWindow."""
    window = ViewerWindow(YouTube())
    window.start()

    def position():
        value = window.player.position_s()
        return None if value is None else round(value, 1)

    def wait_for_home():
        if not window.listed_items:
            return True
        log("home loaded:", len(window.listed_items), "videos; signed in:", window.signed_in)
        GLib.timeout_add_seconds(3, lambda: (screenshot("1-home"), False)[1])
        GLib.timeout_add_seconds(5, play_first)
        return False

    def play_first():
        card = window.grid.get_child_at_index(0)
        log("playing:", card.item.title)
        window.grid.emit("child-activated", card)
        GLib.timeout_add_seconds(8, after_start)
        return False

    def after_start():
        log("position", position(), "title:", window.watch_title.get_text(), "| info:", window.watch_channel.get_text(),
            "| error:", window.watch_error_label.get_text() if window.watch_error_label.get_visible() else None)
        screenshot("2-watch")
        window.player.seek_to(60)
        GLib.timeout_add_seconds(3, after_seek)
        return False

    def after_seek():
        log("after seek to 60 s: position", position(), "| time label:", window.time_label.get_text())
        window.player.toggle_pause()
        GLib.timeout_add_seconds(2, after_pause)
        return False

    def after_pause():
        log("paused: position", position())
        window.player.toggle_pause()
        GLib.timeout_add_seconds(2, go_fullscreen)
        return False

    def go_fullscreen():
        log("resumed: position", position())
        window.toggle_fullscreen()
        GLib.timeout_add_seconds(2, lambda: (log("fullscreen:", window.is_fullscreen, "position", position()), False)[1])
        GLib.timeout_add_seconds(6, in_fullscreen)
        return False

    def in_fullscreen():
        log("controls shown after 4 s idle:", window.controls_revealer.get_reveal_child(), "(expected False)")
        window.toggle_fullscreen()
        GLib.timeout_add_seconds(2, back_to_grid)
        return False

    def back_to_grid():
        log("fullscreen:", window.is_fullscreen, "position", position())
        screenshot("3-watch-after-fullscreen")
        GLib.timeout_add_seconds(1, lambda: (window.show_browse_page(), log("back on", window.pages.get_visible_child_name(), "| player loaded:", window.player.has_video()), False)[1])
        GLib.timeout_add_seconds(3, lambda: (window.destroy(), False)[1])
        return False

    GLib.timeout_add(500, wait_for_home)
    Gtk.main()
    log("closed")


if __name__ == "__main__":
    main()
