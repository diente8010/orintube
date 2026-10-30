"""In-app video player: plays a YouTube HLS stream on the Jetson's hardware decoder inside a GTK widget.

Where it sits: ui.py owns the window and hands this module a Gtk.DrawingArea plus the
two HLS playlist URLs that youtube.py chose. This module builds the GStreamer pipeline,
draws the video into the widget, and offers play/pause/seek/volume.

Pipeline (one process, inside the app):
    souphttpsrc (video .m3u8) -> hlsdemux -> tsdemux -> queue -> h264parse -> nvv4l2decoder (NVDEC) -> nv3dsink
    souphttpsrc (audio .m3u8) -> hlsdemux -> capssetter (AAC) -> aacparse -> avdec_aac -> queue -> audioconvert -> audioresample -> volume -> alsasink
  or, when one playlist carries both (signed-in streams, see youtube.py):
    souphttpsrc (.m3u8) -> hlsdemux -> tsdemux -+-> video chain as above
                                                +-> aacparse -> avdec_aac -> audio chain as above
nv3dsink draws into the DrawingArea's X window through GstVideoOverlay, so the video
fills that widget and goes fullscreen when the window does.

Why HLS and not the DASH files the old `yt` script piped through FIFOs:
    2026-10-19 (user: "it froze. i can hear the audio but its not playing anymore"):
    the FIFO pipeline deadlocked at startup (both yt-dlp writers in pipe_write, gst-launch
    idle in a futex). It ignored Ctrl+C, so its frozen window stayed on top of the next
    video. Plain DASH requests to googlevideo return HTTP 403 unless they are cut into
    &range= chunks, so souphttpsrc cannot read them. YouTube's HLS playlists serve the same
    H.264 as ~5 s MPEG-TS segments that download in about 0.1 s each, and hlsdemux can seek
    in them. Test: Big Buck Bunny 1080p60 played at 29% of one core (the FIFO version: 82%).
    Audio HLS segments are raw ADTS AAC with an ID3 timestamp tag, not MPEG-TS, so
    the audio branch parses AAC directly (see PIPELINE_DESCRIPTION for why the format is fixed).

Threads:
    2026-10-19: every call that can block on GStreamer (start, pause, seek, stop, expose)
    runs on one "control" thread, never on the GTK main thread. seek_stress.py hung on its
    3rd seek with seek_simple() called from the main thread: a flushing seek waits for the
    streaming threads, and nv3dsink's streaming thread was waiting for X/EGL work that
    only the (blocked) main loop could do. One worker keeps the calls in order.

What can go wrong:
    - The widget must be realized (on screen) before load() is called, because nv3dsink
      needs its X window id. Otherwise load() raises RuntimeError.
    - X11 only (GdkX11). The Jetson desktop runs X11 (XDG_SESSION_TYPE=x11).
    - Network stalls: a watchdog reports an error when the position has not moved for
      STALL_TIMEOUT_S seconds while playing.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GdkX11, GLib, Gst, GstVideo, Gtk  # noqa: E402,F401  (GdkX11 provides get_xid)

Gst.init(None)

from .log import log  # noqa: E402

NANOSECONDS_PER_SECOND = Gst.SECOND
POSITION_POLL_INTERVAL_MS = 250
STALL_TIMEOUT_S = 20
SNAPSHOT_DELAY_MS = 150  # after pausing, wait for the last frame to be on screen before copying it
SHUTDOWN_TIMEOUT_S = 3

# 2026-10-19: num-extra-surfaces=6 and enable-last-sample=false fix a seek deadlock.
# seek_stress.py under gdb: the video queue thread was stuck in nvv4l2decoder polling for a free
# input buffer (TegraV4L2_Poll_OPlane). The seek's flush could not wake it, and the seeking
# thread waited forever for that queue (gst_pad_pause_task). Every run hung within 6-10
# random seeks. With the decoder given 6 spare output surfaces and nv3dsink no longer
# holding on to its last frame, 3 runs x 25 seeks all completed, each landing 0-6 s before
# its target (the start of YouTube's ~5 s HLS segment).
#
# 2026-10-19: alsasink (ALSA "default" = "Playback/recording through the PulseAudio sound
# server", so desktop volume and output choice still apply) instead of pulsesink, with
# the sink's own clock. user: "the audio cuts out after the first or second skip", then
# "the audio still only plays when it first start". combo.py (play, 2x pause/resume,
# 4 seeks, counting buffers that reach each sink): pulsesink with its own clock froze the
# video after pause/resume; pulsesink slaved to the system clock (skew, resample or none)
# passed 1-2 of 7 checks: after a seek the audio either raced to the end of the track or
# stopped after ~9 buffers. alsasink passed 7/7 with its own clock and with the system clock.
VIDEO_CHAIN = (
    "queue ! h264parse ! nvv4l2decoder num-extra-surfaces=6 "
    "! nv3dsink name=video_sink enable-last-sample=false"
)
AUDIO_CHAIN = "queue ! audioconvert ! audioresample ! volume name=volume ! alsasink name=audio_sink"

# Separate playlists: video (MPEG-TS) and audio (ID3-tagged ADTS AAC).
# 2026-10-30 (user's screenshot: "Internal data stream error ... The stream kept failing" on an
# NBA highlights video): hlsdemux sniffs each audio segment's bytes to guess its format, and
# for the segment at 624.8 s of AsAPLWibSy4 it guessed "application/ttml+xml" (subtitles), which
# the AAC parser refused ("not-negotiated"), every time and at the same spot, so reconnecting
# could not help. YouTube's audio playlists are always ADTS AAC, so capssetter now overrides
# the guess (join=false: also when the guess is not audio). notneg.py then played that video
# from 10:00 to the end (18:23) without error. aacparse skips the ID3 tag at each segment start.
AUDIO_FORMAT = "audio/mpeg, mpegversion=(int)4, stream-format=(string)adts"
PIPELINE_DESCRIPTION = (
    f"souphttpsrc name=video_source ! hlsdemux ! tsdemux ! {VIDEO_CHAIN} "
    f"souphttpsrc name=audio_source ! hlsdemux ! capssetter join=false replace=true caps=\"{AUDIO_FORMAT}\" "
    f"! aacparse ! avdec_aac ! {AUDIO_CHAIN}"
)
# One playlist whose MPEG-TS segments carry both H.264 and AAC (youtube.py's signed-in try).
MUXED_PIPELINE_DESCRIPTION = (
    f"souphttpsrc name=video_source ! hlsdemux ! tsdemux name=demux "
    f"demux. ! {VIDEO_CHAIN} "
    f"demux. ! aacparse ! avdec_aac ! {AUDIO_CHAIN}"
)


class VideoPlayer:
    """Plays one HLS video+audio pair at a time inside `video_area`.

    video_area: a realized Gtk.DrawingArea that the video is drawn into.
    on_progress(position_s, duration_s): called on the main thread every
        POSITION_POLL_INTERVAL_MS while a video is loaded. duration_s is None for live streams.
    on_state(is_playing): called when playback starts or pauses.
    on_finished(error_text): called once when the video ends (error_text None) or fails
        (error_text is a short message to show the user).
    """

    def __init__(self, video_area, on_progress, on_state, on_finished):
        self.video_area = video_area
        self.on_progress = on_progress
        self.on_state = on_state
        self.on_finished = on_finished
        self.pipeline = None
        self._window_id = None
        self._video_sink_ready = False
        self.volume_fraction = 1.0
        self.muted = False
        self.audio_delay_ms = 0
        self._poll_source_id = None
        self._last_position_s = None
        self._seconds_without_progress = 0.0
        self._is_playing = False
        self._area_size_px = (1, 1)
        self._seek_request_number = 0
        self._control_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gst-control")
        # 2026-10-19: windows with a GTK header bar get a 32-bit (see-through) visual for
        # their rounded corners. The video widget inherited it, and nv3dsink failed with
        # "eglCreateWindowSurface returned 3009" (EGL_BAD_MATCH): black picture, and after a
        # seek the pipeline stayed PAUSED (app_debug.py, Big Buck Bunny). The plain system
        # visual (24-bit, no alpha) is what EGL accepts. It must be set before the widget is realized.
        if video_area.get_realized():
            raise RuntimeError("VideoPlayer must be created before the video widget is shown")
        video_area.set_visual(video_area.get_screen().get_system_visual())
        # Drawing in the widget is done by this class only (see _on_video_area_draw).
        video_area.set_app_paintable(True)
        self._showing_frame = False
        self._paused_snapshot = None
        video_area.connect("size-allocate", self._on_video_area_resized)
        video_area.connect("draw", self._on_video_area_draw)

    # Loading and stopping

    def load(self, video_url, audio_url=None):
        """Stop whatever is playing, then start the given HLS playlists (str URLs).

        audio_url None means the audio is inside the video playlist (MUXED_PIPELINE_DESCRIPTION).

        The picture is stretched to fill the video widget, so the caller keeps the widget
        at the video's aspect ratio (ui.py puts it in a Gtk.AspectFrame).
        Raises RuntimeError if the video widget has no X window yet, or if GStreamer
        cannot build the pipeline (a plugin is missing).
        """
        self.stop()
        gdk_window = self.video_area.get_window()
        if gdk_window is None:
            raise RuntimeError("video area is not on screen yet")
        # 2026-10-19: a GTK3 DrawingArea normally borrows the toplevel's X window, so
        # nv3dsink drew over the whole window and showed only black after fullscreen
        # (screenshot fullscreen.png). ensure_native() gives the widget an X window of its
        # own that moves and resizes with it.
        if not gdk_window.ensure_native():
            raise RuntimeError("video area could not get its own X window")
        # No background: X must not clear the window on expose, or a paused frame is wiped.
        gdk_window.set_background_pattern(None)
        window_id = gdk_window.get_xid()
        self._showing_frame = False
        self._paused_snapshot = None

        try:
            pipeline = Gst.parse_launch(PIPELINE_DESCRIPTION if audio_url else MUXED_PIPELINE_DESCRIPTION)
        except GLib.Error as error:
            raise RuntimeError(f"GStreamer could not build the player: {error.message}") from error
        pipeline.get_by_name("video_source").set_property("location", video_url)
        if audio_url:
            pipeline.get_by_name("audio_source").set_property("location", audio_url)

        self._window_id = window_id
        self._video_sink_ready = False
        bus = pipeline.get_bus()
        # 2026-10-19: nv3dsink segfaults when set_window_handle() is called before it is
        # in READY (gdb: SIGSEGV inside libgstnvvideosinks.so from load()). The handle is
        # therefore handed over when the sink asks for it with "prepare-window-handle".
        # That message comes from the streaming thread, so it needs a sync handler.
        bus.enable_sync_message_emission()
        bus.connect("sync-message::element", self._on_sync_element_message)
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::eos", self._on_bus_end_of_stream)
        bus.connect("message::state-changed", self._on_bus_state_changed)

        # 2026-10-19: the pipeline uses alsasink's clock. An earlier version forced the system
        # clock to work around pulsesink reading position 0 after pause/resume (user: "it
        # was playing your timing off"), but that broke the audio after seeks. With alsasink
        # (see PIPELINE_DESCRIPTION) neither problem happens, so nothing is overridden.
        self.pipeline = pipeline
        self._apply_volume()
        self._apply_audio_delay()
        self._last_position_s = None
        self._seconds_without_progress = 0.0
        self._control_thread.submit(pipeline.set_state, Gst.State.PLAYING)
        self._poll_source_id = GLib.timeout_add(POSITION_POLL_INTERVAL_MS, self._poll_position)

    def stop(self):
        """Stop and throw away the current pipeline. Safe to call when nothing is loaded.

        Does not call on_finished: stopping on purpose is not the video "finishing".
        """
        if self._poll_source_id is not None:
            GLib.source_remove(self._poll_source_id)
            self._poll_source_id = None
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is None:
            return
        pipeline.get_bus().remove_signal_watch()
        # 2026-10-19: going straight to NULL. Sending EOS first (what gst-launch -e does)
        # hung for good with hlsdemux in the gst-launch test, and nothing needs to be
        # finalized for a stream that is only being watched.
        self._control_thread.submit(pipeline.set_state, Gst.State.NULL)
        self._set_playing(False)

    def has_video(self):
        """Return True while a video is loaded (playing or paused)."""
        return self.pipeline is not None

    # Controls

    def toggle_pause(self):
        """Pause if playing, resume if paused. Does nothing when no video is loaded."""
        if self.pipeline is None:
            return
        target = Gst.State.PAUSED if self._is_playing else Gst.State.PLAYING
        self._control_thread.submit(self.pipeline.set_state, target)

    def seek_to(self, position_s):
        """Jump to `position_s` seconds from the start (clamped to the video length).

        Jumps to the nearest keyframe (KEY_UNIT), which for YouTube is at most a couple of
        seconds away, because that seeks much faster over the network than an exact seek.
        """
        if self.pipeline is None:
            return
        duration_s = self.duration_s()
        position_s = max(0.0, position_s)
        if duration_s is not None:
            position_s = min(position_s, max(0.0, duration_s - 1.0))
        self._seek_request_number += 1
        self._control_thread.submit(self._seek_in_control_thread, self.pipeline,
                                    int(position_s * NANOSECONDS_PER_SECOND), self._seek_request_number)
        self._last_position_s = None
        self._seconds_without_progress = 0.0

    def seek_by(self, delta_s):
        """Jump `delta_s` seconds forward (positive) or back (negative) from the current position."""
        position_s = self.position_s()
        if position_s is not None:
            self.seek_to(position_s + delta_s)

    def set_volume(self, fraction):
        """Set the volume, 0.0 (silent) to 1.0 (full). Kept across videos."""
        self.volume_fraction = min(1.0, max(0.0, fraction))
        self._apply_volume()

    def set_audio_delay(self, delay_ms):
        """Hold the sound back by `delay_ms` milliseconds (negative = earlier). Kept across videos.

        2026-10-22 (user: "the audio is about a half second faster than the video"): inside
        GStreamer both arrive on time (av_lateness.py: video 16 ms, audio 23 ms early), and
        sound and picture both go to the display over HDMI/DP; TVs often delay the picture
        for processing without delaying the sound. Like VLC and mpv, the app lets the
        viewer shift the sound; it is applied as the audio sink's ts-offset.
        """
        self.audio_delay_ms = int(delay_ms)
        self._apply_audio_delay()

    def toggle_mute(self):
        """Mute or unmute without losing the volume setting. Returns True when now muted."""
        self.muted = not self.muted
        self._apply_volume()
        return self.muted

    def position_s(self):
        """Return the playback position in seconds, or None when unknown or nothing is loaded."""
        if self.pipeline is None:
            return None
        found, position_ns = self.pipeline.query_position(Gst.Format.TIME)
        return position_ns / NANOSECONDS_PER_SECOND if found and position_ns >= 0 else None

    def duration_s(self):
        """Return the video length in seconds, or None for live streams or when unknown."""
        if self.pipeline is None:
            return None
        found, duration_ns = self.pipeline.query_duration(Gst.Format.TIME)
        return duration_ns / NANOSECONDS_PER_SECOND if found and duration_ns > 0 else None

    def shutdown(self, timeout_s=SHUTDOWN_TIMEOUT_S):
        """Stop playback and wait at most timeout_s seconds for it to finish. Call when the app closes.

        2026-10-19 review: waiting without a limit could hang the closing window forever if
        a GStreamer call were stuck. Returns True when everything stopped in time.
        """
        self.stop()
        finished = threading.Event()
        self._control_thread.submit(finished.set)
        self._control_thread.shutdown(wait=False)
        return finished.wait(timeout_s)

    # Internals

    def _seek_in_control_thread(self, pipeline, position_ns, request_number):
        """Control thread: perform a keyframe seek, unless a newer seek was asked for meanwhile.

        Dragging the seek bar can queue several seeks. Only the latest one is worth the
        network round trip.
        """
        if request_number != self._seek_request_number:
            return
        flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
        pipeline.seek_simple(Gst.Format.TIME, flags, position_ns)

    def _apply_audio_delay(self):
        """Push the audio delay into the audio sink, if a video is loaded."""
        if self.pipeline is not None:
            self.pipeline.get_by_name("audio_sink").set_property("ts-offset", self.audio_delay_ms * Gst.MSECOND)

    def _apply_volume(self):
        """Push the volume/mute setting into the pipeline's volume element, if a video is loaded."""
        if self.pipeline is None:
            return
        volume_element = self.pipeline.get_by_name("volume")
        volume_element.set_property("volume", self.volume_fraction)
        volume_element.set_property("mute", self.muted)

    def _on_sync_element_message(self, _bus, message):
        """Bus, streaming thread: when nv3dsink asks for a window, give it the video widget's X window."""
        if not GstVideo.is_video_overlay_prepare_window_handle_message(message):
            return
        video_sink = message.src
        video_sink.set_window_handle(self._window_id)
        width_px, height_px = self._area_size_px  # GTK must not be called from this thread
        video_sink.set_render_rectangle(0, 0, width_px, height_px)
        self._video_sink_ready = True
        video_sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_first_frame)

    def _on_video_area_resized(self, _widget, allocation):
        """Keep the video filling the widget when the window is resized or goes fullscreen.

        2026-10-19: nv3dsink ignores the rectangle and always fills its window (a 906x585
        widget showed a full-height 16:9 picture, stretched), so the aspect ratio is kept by
        sizing the widget itself (ui.py's AspectFrame), not here.
        """
        self._area_size_px = (allocation.width, allocation.height)
        if self.pipeline is None or not self._video_sink_ready:
            return
        self._control_thread.submit(self._redraw_in_control_thread, self.pipeline.get_by_name("video_sink"),
                                    allocation.width, allocation.height)
        if self._paused_snapshot is not None:
            self.video_area.queue_draw()  # repaint the paused frame at the new size

    def _redraw_in_control_thread(self, video_sink, width_px, height_px):
        """Control thread: tell nv3dsink the new widget size and redraw the last frame (expose can block)."""
        video_sink.set_render_rectangle(0, 0, width_px, height_px)
        video_sink.expose()

    def _on_first_frame(self, _pad, _info):
        """Streaming thread, once per video: the first frame reached nv3dsink, so stop painting black."""
        self._showing_frame = True
        return Gst.PadProbeReturn.REMOVE

    def _on_video_area_draw(self, _widget, cairo_context):
        """Paint the widget black until the first frame arrives; after that, never paint over the video.

        2026-10-22 (user: "when i ever i pause a video, the screen goes black instead of freezing
        the image like a normal player would"): pausing kept the frame, but any redraw while
        paused (pause_black.py: queue_draw 2 s after pausing) took the video area from brightness
        235 to 0, because this handler painted black and nv3dsink does not repaint a paused frame,
        with or without enable-last-sample. Not painting (plus no window background) kept 235.
        """
        if self._paused_snapshot is not None and not self._is_playing:
            allocation = self.video_area.get_allocation()
            scaled = self._paused_snapshot.scale_simple(max(1, allocation.width), max(1, allocation.height),
                                                        GdkPixbuf.InterpType.BILINEAR)
            Gdk.cairo_set_source_pixbuf(cairo_context, scaled, 0, 0)
            cairo_context.paint()
            return True
        if self._showing_frame:
            return True
        cairo_context.set_source_rgb(0, 0, 0)
        cairo_context.paint()
        return True

    def _poll_position(self):
        """Timer (every POSITION_POLL_INTERVAL_MS): report progress and watch for stalls."""
        if self.pipeline is None:
            return False
        position_s = self.position_s()
        self.on_progress(position_s, self.duration_s())
        if self._is_playing:
            self._check_for_stall(position_s)
        return True

    def _check_for_stall(self, position_s):
        """End playback with an error when the position has not moved for STALL_TIMEOUT_S seconds while playing."""
        if position_s is not None and position_s != self._last_position_s:
            self._last_position_s = position_s
            self._seconds_without_progress = 0.0
            return
        self._seconds_without_progress += POSITION_POLL_INTERVAL_MS / 1000
        if self._seconds_without_progress >= STALL_TIMEOUT_S:
            log(f"player stalled at {self._last_position_s} s")
            self._finish(f"Playback stalled: no progress for {STALL_TIMEOUT_S} s (network?)")

    def _set_playing(self, is_playing):
        """Record the playing/paused state and tell the UI when it changes."""
        if is_playing != self._is_playing:
            self._is_playing = is_playing
            self.on_state(is_playing)
        if is_playing:
            self._paused_snapshot = None
        elif self.pipeline is not None and self._showing_frame:
            GLib.timeout_add(SNAPSHOT_DELAY_MS, self._take_paused_snapshot)

    def _take_paused_snapshot(self):
        """Timer, just after pausing: copy the frame shown on screen, to repaint it while paused.

        2026-10-22 (user: pausing turned the screen black): nv3dsink does not redraw a paused
        frame, so a resize or fullscreen while paused showed black or old screen contents. A
        flushing seek while paused, to make GStreamer draw the frame again, hung the player
        (pause_app.py: the position stayed at 64.78 s after resuming). Copying the pixels
        from the screen and painting them with cairo needs no GStreamer call at all.
        """
        gdk_window = self.video_area.get_window()
        if self.pipeline is None or self._is_playing or gdk_window is None:
            return False
        allocation = self.video_area.get_allocation()
        _found, x, y = gdk_window.get_origin()
        self._paused_snapshot = Gdk.pixbuf_get_from_window(Gdk.get_default_root_window(), x, y,
                                                           allocation.width, allocation.height)
        return False

    def _on_bus_state_changed(self, _bus, message):
        """Bus: track the whole pipeline's PLAYING/PAUSED state (messages from single elements are ignored)."""
        if message.src is not self.pipeline:
            return
        _old, new, _pending = message.parse_state_changed()
        self._set_playing(new == Gst.State.PLAYING)

    def _on_bus_error(self, _bus, message):
        """Bus: an element failed. Stop and report the error text."""
        error, debug = message.parse_error()
        log(f"player error from {message.src.get_name()}: {error.message} | {debug}")
        self._finish(error.message)

    def _on_bus_end_of_stream(self, _bus, _message):
        """Bus: both video and audio reached their end. Stop and report a normal finish."""
        self._finish(None)

    def _finish(self, error_text):
        """Stop the pipeline, then call on_finished(error_text) once."""
        self.stop()
        self.on_finished(error_text)
