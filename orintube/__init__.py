"""OrinTube for the Jetson Orin Nano: YouTube-style browsing with hardware-decoded playback.

Modules: youtube.py (feeds, search, streams), player.py (GStreamer + NVDEC in a GTK widget),
thumbnails.py (thumbnail downloads), ui.py (the window), x11threads.py (must load first).
Run with: orintube   (or: python3 -m orintube from ~/orintube)
"""
