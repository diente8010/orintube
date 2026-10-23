<p align="center"><img src="data/orintube.svg" width="128" alt="OrinTube icon"></p>

# OrinTube

**Smooth YouTube on NVIDIA Jetson, decoded by the board's video hardware instead of its ARM CPU.**

OrinTube is a desktop app for Jetson boards (developed on a Jetson Orin Nano) that lets you
browse and watch YouTube with a familiar dark, YouTube-style layout, while the video itself is
decoded by **NVDEC**, the dedicated hardware video decoder in the Jetson's NVIDIA graphics
hardware.

> OrinTube is an unofficial, independent project. It is not affiliated with, endorsed by or
> sponsored by YouTube or Google. "YouTube" is a trademark of Google LLC and is used here only
> to describe what the app works with. See [Legal and privacy](#legal-and-privacy).

## Why it exists

Jetson boards run Ubuntu on ARM CPU cores. Web browsers on Jetson can't use the board's
hardware video decoder, so every frame of a YouTube video is decoded in software on the ARM
cores and then copied into the browser's compositor. YouTube also gives browsers its heaviest
codecs (AV1 or VP9). The result is high CPU load and stuttering as soon as anything else
needs the CPU.

OrinTube asks YouTube for H.264 streams, which the Jetson's NVDEC decodes in hardware. It hands
the decoded frames straight to the screen through NVIDIA's own GStreamer elements
(`nvv4l2decoder` → `nv3dsink`), without copying them through the CPU.

### Measured on a Jetson Orin Nano 8 GB (JetPack 6.2, MAXN SUPER), 1080p60 video

Same video (*Big Buck Bunny 60fps*), same machine, same day:

| | Codec | Frames shown | CPU used by the player |
|---|---|---|---|
| Web browser (Chromium-based) | AV1, software | 60 fps | about 2.3 of 6 cores; whole system 45% busy |
| **OrinTube** | H.264, **NVDEC hardware** | **60 fps, 0 dropped** | **about 0.5 of one core; whole system 26% busy** |

So OrinTube plays the same 1080p60 video with **roughly a quarter of the CPU**. That leaves the
ARM cores free, so playback stays smooth when other programs run. The browser kept up in this
quiet test, but with little headroom left, which is when stutter starts.

The decoders on their own (decode only, no display, same stream, CPU time per second):

| Decoder | Real time 1080p60 | Flat out |
|---|---|---|
| H.264 in hardware (NVDEC) | 42% of one core | 242 fps |
| H.264 in software (FFmpeg) | 122% of one core | 226 fps using 3.4 cores |
| VP9 in hardware (NVDEC) | 30% of one core | 396 fps |
| VP9 in software (FFmpeg) | 114% of one core | 239 fps using 3.2 cores |

## What it does

- **Browse like YouTube:** a dark grid of thumbnails with titles, channels, view counts and
  lengths. Sections: **Home** (your recommendations), **Subscriptions**, **History**,
  **Watch later** and **Shorts** (the newest Shorts from your subscribed channels).
  Scroll down to load more.
- **Search**, or paste any YouTube link (`watch?v=`, `youtu.be/`, `shorts/`, `live/`), including
  links with a start time (`t=1m30s`).
- **Watch page:** play/pause, next, seek bar, volume, quality (1080p down to 360p), real
  **fullscreen** with controls that hide after 3 seconds.
- **Up next:** YouTube's related videos for what you are watching, with autoplay.
- **Comments:** the top 40 comments of the video (read-only; see below).
- **Shorts** play in their vertical shape; live streams play live.
- **Fallback:** if a video can't play here, one button opens it in your browser.

### Keyboard

| Key | Action |
|---|---|
| F or double-click | fullscreen on/off |
| Space or K | pause/play |
| ← / → | 5 s back / forward |
| J / L | 10 s back / forward |
| 0–9 | jump to 0%–90% |
| Home | back to the start |
| ↑ / ↓ | volume |
| M | mute |
| [ / ] | sound 50 ms earlier / later (saved), for TVs that delay the picture |
| Shift+N / Shift+P | next / previous video |
| Esc | leave fullscreen, then back to the grid |
| / | search box |
| F5 or Ctrl+R | refresh the list (it also refreshes itself when you come back after 10 minutes) |

## Install

Needs a Jetson with JetPack 6 (Ubuntu 22.04, X11 desktop). Everything installs for your user
only; sudo is only needed if system packages are missing, and the installer tells you which.

```bash
git clone https://github.com/diente8010/orintube.git ~/orintube
cd ~/orintube
./install.sh --with-deno
```

`install.sh`:
1. checks NVIDIA's GStreamer elements, the GStreamer plugin sets, PyGObject and pip;
2. installs or updates `yt-dlp` and `yt-dlp-ejs` with `pip --user`;
3. with `--with-deno`, downloads [deno](https://deno.com) from its official GitHub releases and
   checks its published SHA-256 (yt-dlp needs a JavaScript runtime for some videos);
4. adds the `orintube` command, the icon, and **OrinTube** to the app grid.

Then open **OrinTube** from the app grid (pin it to the dock with right-click → Add to
Favorites), or run `orintube` or `orintube <youtube-link>`.

**Your own feeds:** sign in to YouTube once in Chromium or Google Chrome on the same Jetson.
OrinTube reuses that sign-in (see below). The account button (top right) can open the sign-in
page and re-read it.

## How it works

1. `yt-dlp` (as a Python library) lists your feeds and search results and finds each video's
   **HLS** streams: H.264 video and AAC audio, in ~5-second segments.
2. GStreamer plays them inside the window:
   `souphttpsrc → hlsdemux → tsdemux → h264parse → nvv4l2decoder → nv3dsink` for video, and
   AAC decoding into `alsasink` (ALSA's default device, which goes through PulseAudio).
3. The video is drawn straight into the app's window (GstVideoOverlay), which is what makes
   real fullscreen and the black bars around Shorts possible.

If YouTube won't give a video's streams signed out, OrinTube asks again with your sign-in.
`STATUS.md` records every design decision and the test that justified it.

## Legal and privacy

**What OrinTube is:** an independent, unofficial client. It is not made, endorsed or supported
by YouTube or Google. It has its own name and icon and doesn't use YouTube's logo.

**What it does not do:**
- It does **not download or save videos**. It only streams them to the screen while you
  watch, as a browser does. There is no download button and nothing is written to disk.
- It does **not post anything** to YouTube: no comments, likes or subscriptions. Comments
  are read-only on purpose. Posting would mean driving YouTube's private web interface
  with your browser cookies, which YouTube's terms don't allow and which could put your
  account at risk. The **Add a comment** button opens the video in your browser instead.
- It does **not** bypass paywalls or DRM. Paid and members-only content plays only if
  YouTube serves it to your own signed-in account.

**Your account and data:**
- OrinTube never asks for, sees or stores your Google password.
- For your personal feeds it reads the YouTube cookies of your own browser on the same
  machine, read-only, exactly as `yt-dlp --cookies-from-browser` does. The cookies stay in
  memory and are never written anywhere or sent anywhere except to YouTube.
- Run `orintube --no-sign-in` to skip that and use search only.

**Network connections it makes:** YouTube (`youtube.com`, `googlevideo.com`) for pages and
video, `i.ytimg.com` for thumbnails, and, only when you run `install.sh`, PyPI (for yt-dlp)
and GitHub (for deno). Nothing else, and no analytics or telemetry.

**Your responsibility:** using any third-party YouTube client may conflict with
[YouTube's Terms of Service](https://www.youtube.com/t/terms). Use OrinTube for personal
viewing, respect creators' rights, and don't use it to copy or redistribute content.
OrinTube is provided "as is", without warranty (see [LICENSE](LICENSE)).

OrinTube builds on [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense), which is installed
separately and not included in this repository.

## Tests

```bash
python3 -m unittest discover -s tests -v      # logic only; no network, screen or decoder needed
python3 tests/player_check.py                 # on the Jetson: pause and seek, checking audio + video
python3 tests/smoke_ui.py /tmp                # on the Jetson: scripted run of the whole app
```

## Limits

- Jetson with JetPack 6 only (NVIDIA's `nvv4l2decoder` and `nv3dsink`), X11 desktop.
- H.264, up to 1080p (YouTube's H.264 stops at 1080p).
- Skips land up to ~5 s before the chosen point (YouTube's HLS segments are ~5 s long).
- Videos that need the signed-in fallback take 12–16 s to start.
- YouTube changes break third-party clients from time to time. Re-running `./install.sh`
  updates yt-dlp, which usually fixes it.
- yt-dlp has announced the end of support for Python 3.10 (JetPack 6's Python). A future
  yt-dlp update may need Python 3.11+.

## License

MIT. See [LICENSE](LICENSE).
