"""Player check: play, pause/resume and seek Big Buck Bunny, and verify audio AND video keep flowing.

Needs the Jetson desktop, network and sound. About 32 s. Prints OK/BAD per step and a RESULT
line; exit status 0 only when every step is OK.
Run:  python3 ~/orintube/tests/player_check.py [video-id] [--muxed]

Each check counts the buffers that reached nv3dsink and the volume element since the last
check: video must have > 50 frames, audio 30-2000 buffers (2000+ means it raced instead of
playing in real time), audio and video timestamps within 1 s of each other, and the position
within 7 s of where it should be. Written 2026-10-19 to catch the audio dying after seeks
(user: "the audio cuts out after the first or second skip"); see PIPELINE_DESCRIPTION in player.py.
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import orintube.x11threads
from orintube import player as P
from orintube.youtube import YouTube
from gi.repository import Gtk, GLib, Gst
# Arguments: [video id, default Big Buck Bunny] [--muxed = use the signed-in single-playlist streams]
arguments = [a for a in sys.argv[1:] if not a.startswith("--")]
video_id = arguments[0] if arguments else "aqz-KE-bpKQ"
if "--muxed" in sys.argv:
    s = YouTube()._get_muxed_streams(video_id, 1080)
else:
    s = YouTube(use_sign_in=False).get_streams(video_id, 1080)
print("streams:", s.title[:50], f"{s.height_px}p", "muxed" if s.audio_url is None else "separate", flush=True)
win = Gtk.Window(); win.set_default_size(640, 360); area = Gtk.DrawingArea(); win.add(area)
pl = P.VideoPlayer(area, lambda p, d: None, lambda x: None, lambda e: print("finished", e, flush=True))
win.show_all()
t0 = time.monotonic(); c = {"a": 0, "v": 0, "apts": 0, "vpts": 0}; results = []
def log(*a): print(f"{time.monotonic()-t0:5.1f}s", *a, flush=True)
def attach():
    p = pl.pipeline
    def mk(k):
        def cb(pad, info):
            c[k] += 1; c[k + "pts"] = info.get_buffer().pts / 1e9; return Gst.PadProbeReturn.OK
        return cb
    p.get_by_name("video_sink").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, mk("v"))
    p.get_by_name("volume").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, mk("a"))
def check(label, expect_s):
    pos = pl.position_s()
    ok = c["v"] > 50 and 30 < c["a"] < 2000 and abs(c["vpts"] - c["apts"]) < 1.0 and pos is not None and abs(pos - c["vpts"]) < 1.0 and abs(c["vpts"] - expect_s) < 7
    results.append(ok)
    log(f"{'OK ' if ok else 'BAD'} {label}: pos {pos and round(pos,1)}  video {c['v']} frames @ {c['vpts']:.1f}  audio {c['a']} bufs @ {c['apts']:.1f}")
    c["a"] = c["v"] = 0
steps = [(1, lambda: (pl.load(s.video_url, s.audio_url), GLib.timeout_add(300, lambda: (attach(), False)[1]))),
         (5, lambda: check("play", 3)),
         (5.05, pl.toggle_pause), (7, lambda: (c.update(a=0, v=0), pl.toggle_pause())), (10, lambda: check("after pause/resume", 6)),
         (10.05, lambda: pl.seek_to(200)), (14, lambda: check("after seek 200", 203)),
         (14.05, lambda: pl.seek_to(450)), (18, lambda: check("after seek 450", 453)),
         (18.05, lambda: pl.seek_to(100)), (22, lambda: check("after seek 100", 103)),
         (22.05, pl.toggle_pause), (24, lambda: (c.update(a=0, v=0), pl.toggle_pause())), (27, lambda: check("after 2nd pause/resume", 106)),
         (27.05, lambda: pl.seek_to(300)), (31, lambda: check("after seek 300", 303)),
         (31.5, lambda: (log("RESULT", f"{sum(results)}/{len(results)} OK"), pl.stop(), Gtk.main_quit()))]
for at, fn in steps: GLib.timeout_add(int(at * 1000), lambda fn=fn: (fn(), False)[1])
Gtk.main()
os._exit(0 if all(results) else 1)
