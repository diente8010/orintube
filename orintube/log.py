"""Writes OrinTube's diagnostic log to ~/.cache/orintube/orintube.log (rotated, at most ~3 MB).

Where it sits: youtube.py, player.py and ui.py call log() for things worth knowing after the
fact: failed stream lookups (and which try failed), GStreamer errors with their debug detail,
and automatic recoveries.
2026-10-23 (user: "the player is also saying internal data stream error"): GStreamer's message
was shown on screen but kept nowhere, and the system journal had only yt-dlp's output, so
the cause could not be traced afterwards.

What can go wrong: if the log cannot be written (disk full, no home folder), logging is
silently skipped; it never stops playback.
"""

import logging
import logging.handlers
import os

LOG_FILE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "orintube", "orintube.log")
LOG_FILE_MAX_BYTES = 1024 * 1024
LOG_FILE_BACKUPS = 2

_logger = logging.getLogger("orintube")


def _setup():
    """Attach the rotating file handler once. Falls back to no logging when the file can't be opened."""
    if _logger.handlers:
        return
    _logger.setLevel(logging.INFO)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS)
    except OSError:
        _logger.addHandler(logging.NullHandler())
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _logger.addHandler(handler)


def log(message):
    """Append one line (str) to the log, with a timestamp."""
    _setup()
    _logger.info(message)
