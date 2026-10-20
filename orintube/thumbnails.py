"""Downloads video thumbnails in the background and hands them to GTK images with rounded corners.

Where it sits: ui.py asks for a thumbnail for each card and Up next row. This module
downloads them (mqdefault.jpg, 320x180, about 15 KB; Shorts covers oar2.jpg, 1080x1920,
about 230 KB) on a small thread pool and keeps the downloaded bytes in memory, up to
CACHE_LIMIT_BYTES, dropping the least recently used first. It writes no files.

What can go wrong: a failed download (no network, removed video) leaves the image
blank. No error is shown, because a missing thumbnail is not worth interrupting for.
"""

import math
import threading
from collections import OrderedDict
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

DOWNLOAD_WORKERS = 6
DOWNLOAD_TIMEOUT_S = 10
CORNER_RADIUS_PX = 10
CACHE_LIMIT_BYTES = 48 * 1024 * 1024


def round_corners(pixbuf, radius_px):
    """Return a copy of `pixbuf` with transparent rounded corners of `radius_px` pixels, like YouTube's cards."""
    width, height = pixbuf.get_width(), pixbuf.get_height()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    context.new_sub_path()
    context.arc(width - radius_px, radius_px, radius_px, -math.pi / 2, 0)
    context.arc(width - radius_px, height - radius_px, radius_px, 0, math.pi / 2)
    context.arc(radius_px, height - radius_px, radius_px, math.pi / 2, math.pi)
    context.arc(radius_px, radius_px, radius_px, math.pi, 3 * math.pi / 2)
    context.close_path()
    context.clip()
    Gdk.cairo_set_source_pixbuf(context, pixbuf, 0, 0)
    context.paint()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, width, height)


def decode_image(image_bytes, width_px, height_px):
    """Decode JPEG/PNG bytes into a GdkPixbuf of width_px x height_px. Returns None when the bytes are not an image.

    2026-10-19: the loader is told the target size before decoding, so a 1080x1920 Shorts
    cover is shrunk while it is decoded instead of after (JPEG can do that cheaply).
    """
    loader = GdkPixbuf.PixbufLoader()
    loader.connect("size-prepared", lambda loader, _width, _height: loader.set_size(width_px, height_px))
    try:
        loader.write(image_bytes)
        loader.close()
    except GLib.Error:
        return None
    return loader.get_pixbuf().scale_simple(width_px, height_px, GdkPixbuf.InterpType.BILINEAR)


class ThumbnailLoader:
    """Fills Gtk.Image widgets with thumbnails, downloading each URL once per app run."""

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)
        self._bytes_by_url = OrderedDict()
        self._cached_bytes = 0
        self._lock = threading.Lock()

    def load_into(self, image_widget, url, width_px, height_px):
        """Show the thumbnail at `url` in `image_widget`, scaled to width_px x height_px with rounded corners.

        Returns at once. The image appears when the download finishes, or straight away if cached.
        """
        with self._lock:
            cached = self._bytes_by_url.get(url)
            if cached is not None:
                self._bytes_by_url.move_to_end(url)
        if cached is not None:
            self._show(image_widget, cached, width_px, height_px)
            return
        self._pool.submit(self._download, image_widget, url, width_px, height_px)

    def shutdown(self):
        """Drop downloads that have not started yet. Called when the app closes."""
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _download(self, image_widget, url, width_px, height_px):
        """Worker thread: fetch the bytes, cache them, then display them on the main thread."""
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response:
                image_bytes = response.read()
        except OSError:
            return
        self._remember(url, image_bytes)
        GLib.idle_add(self._show, image_widget, image_bytes, width_px, height_px)

    def _remember(self, url, image_bytes):
        """Cache one download, then drop the least recently used ones beyond CACHE_LIMIT_BYTES."""
        with self._lock:
            if url in self._bytes_by_url:
                return
            self._bytes_by_url[url] = image_bytes
            self._cached_bytes += len(image_bytes)
            while self._cached_bytes > CACHE_LIMIT_BYTES and len(self._bytes_by_url) > 1:
                _oldest_url, oldest_bytes = self._bytes_by_url.popitem(last=False)
                self._cached_bytes -= len(oldest_bytes)

    def _show(self, image_widget, image_bytes, width_px, height_px):
        """Main thread: decode, round the corners and put the picture in the widget."""
        pixbuf = decode_image(image_bytes, width_px, height_px)
        if pixbuf is not None:
            image_widget.set_from_pixbuf(round_corners(pixbuf, CORNER_RADIUS_PX))
        return False
