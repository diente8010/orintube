"""Remembers the viewer's choices between runs, in ~/.config/orintube/settings.json.

Where it sits: ui.py reads the settings at start-up and saves them when one changes.
Keys (all optional; missing keys take the defaults below):
    audio_delay_ms  int, milliseconds the sound is held back to match the picture
                    (positive = later). For TVs that delay the picture for processing.

What can go wrong: an unreadable or broken file is ignored (defaults are used) and is
overwritten on the next save.
"""

import json
import os

SETTINGS_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                             "orintube", "settings.json")
DEFAULTS = {"audio_delay_ms": 0}


def load_settings(path=SETTINGS_FILE):
    """Return the saved settings (dict) merged over DEFAULTS. Never raises."""
    settings = dict(DEFAULTS)
    try:
        with open(path) as settings_file:
            saved = json.load(settings_file)
        if isinstance(saved, dict):
            settings.update({key: value for key, value in saved.items() if key in DEFAULTS})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings, path=SETTINGS_FILE):
    """Write `settings` (dict) to `path`, creating the folder. Errors are ignored (settings are a convenience)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as settings_file:
            json.dump(settings, settings_file, indent=2)
    except OSError:
        pass
