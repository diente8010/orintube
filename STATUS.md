# OrinTube: status

YouTube-style app for the Jetson Orin Nano (JetPack 6.2, R36.5) that plays video on the
hardware decoder (NVDEC). Start it from the app grid ("OrinTube") or run `orintube [link]`.

## Layout
- `orintube/youtube.py`: feeds (with the browser's sign-in), Shorts, search, comments, related videos, HLS stream choice (yt-dlp library)
- `orintube/player.py`: GStreamer pipeline inside the window (hlsdemux, nvv4l2decoder, nv3dsink, alsasink)
- `orintube/ui.py`: window, browse grid, watch page, fullscreen, keys
- `orintube/thumbnails.py`, `orintube/x11threads.py`, `orintube/__main__.py`
- `tests/test_logic.py`: unit tests, no network: `python3 -m unittest discover -s ~/orintube/tests -v`
- `tests/player_check.py`: on-screen pause/seek check of audio + video (exit 0 = all OK)
- `tests/smoke_ui.py`: scripted run of the whole app, with screenshots
- `bin/orintube` (linked into ~/.local/bin by install.sh), `data/orintube.desktop.in`, `data/orintube.svg`

## 2026-10-19: first version (replaces the `yt` FIFO script and the list-only viewer)
What changed and why, with the evidence:
- **Player moved into the app, on HLS.** The FIFO `yt` script deadlocked at startup now and then
  (both yt-dlp writers in pipe_write, gst-launch idle), and the stuck process ignored Ctrl+C.
  user: "it froze. i can hear the audio but its not playing anymore". DASH URLs return
  HTTP 403 to plain requests, while HLS segments download in ~0.1 s and can be seeked.
  Big Buck Bunny 1080p60 used 29% of one core (FIFO version: 82%).
- **Real fullscreen**: the video draws into the window (GstVideoOverlay), so F / double-click / the
  button makes it fullscreen. Fixes needed: XInitThreads (core dump), prepare-window-handle
  (segfault in nv3dsink), ensure_native (black after fullscreen), system visual
  (EGL_BAD_MATCH = black picture), AspectFrame (nv3dsink stretches the picture).
- **Seek deadlock**: blocking calls moved off the GTK thread, plus `num-extra-surfaces=6` and
  `enable-last-sample=false`. Before: every seek_stress run hung within 6-10 seeks. After:
  4 runs x 25 seeks, no hang.
- **Audio dying after seeks** (user: "the audio cuts out after the first or second skip"):
  pulsesink fails here in every clock setup (with its own clock the video froze after
  pause/resume; slaved to the system clock, 1-2 of 7 checks passed). alsasink, which still goes
  through PulseAudio, passed 7/7 in 4 runs (player_check.py).
- **Sign-in** through Chromium's cookies (the user chose this): Home, Subscriptions, History and
  Watch later load in 1-2 s each.

## 2026-10-19: deno + signed-in fallback
- The user said "yes" to installing deno 2.9.7 (aarch64 zip from denoland/deno releases, sha256
  c832298b..., in ~/.deno/bin, linked as ~/.local/bin/deno) and yt-dlp-ejs 0.8.0 (pip --user).
- The SpongeBob compilation that failed ("This video is not available") then resolved, but
  signed out it only offered DASH. Signed in with the web_safari client it offers HLS with
  video+audio in one playlist, so youtube.py tries signed out first (~2 s) and falls back to
  signed in (~12-16 s total); player.py plays that single playlist (MUXED_PIPELINE_DESCRIPTION).
- player_check.py 7/7 for: SpongeBob (muxed), Big Buck Bunny (muxed), Big Buck Bunny (separate).
- The play button now shows "play" after a failed video (the user's screenshot: "nothing is playing").

## 2026-10-19: shareable repository
- User: "can you tern this into a repo or am I the only one allowed to use this".
- The browser is detected (Chromium snap, .deb Chromium, Google Chrome) instead of the
  Chromium snap path being fixed in the code (youtube.find_browser, tested in test_logic.py).
- Launchers moved into bin/ and find the repository from their own location; install.sh
  checks the system, installs yt-dlp + yt-dlp-ejs, optionally deno (--with-deno, checksum
  verified), links the commands and adds the app-grid entry. Run on this Jetson: all OK.
- README.md for other users.

## 2026-10-19: Up next, icon, launcher
- User: "the up next section is missing. there isnt an icon either. i dont know how to open
  the app you just made".
- Up next now shows YouTube's related videos, read from the watch page's ytInitialData
  (lockupViewModel items; 20 for a SpongeBob compilation, ~1 s). It used to show only the
  rest of the list the video was clicked in, which is empty for a pasted link.
- The window was called "__main__.py", so GNOME could not match it to its launcher and
  showed no icon. GLib.set_prgname("orintube") + StartupWMClass=orintube fixed it
  (xprop: WM_CLASS "orintube"). New icon data/orintube.svg, installed by install.sh.
- Pinned to the dock on this Jetson (added 'orintube.desktop' to org.gnome.shell favorite-apps).
- 20 unit tests pass. Seen on screen: the NBA game playing, Up next with related games, icon in dock and top bar.

## 2026-10-19: improvement pass, Shorts, comments, rebrand to OrinTube
- User: "do an improvement pass ... once thats done you can post to my github public".
- A separate review found 8 defects, all fixed: the browse grid could stay empty after a quick
  section change; the old video kept playing (and could autoplay over a new choice) while the
  next one loaded; keyboard shortcuts went into the search box after pasting a link; closing could
  hang on a stuck GStreamer call; abandoned slow lookups queued up; search paging stopped early;
  sign-in errors were misreported; install.sh missed pip/GstVideo/cairo checks, left temp files
  on a failed download, and had an unquoted Exec path.
- Added from the review: t= start times in links, keys 0-9 / Home, Shift+P for the previous video,
  next page loads when the first does not fill the window.
- Shorts quality uses the shorter side (a 1080p Short is 1080x1920; it was capped at 854p).
- User: "no youtube shorts either": Shorts section (Shorts tabs of subscribed channels; the feeds
  have none). Covers via oar2.jpg because the listed covers are WebP, which GdkPixbuf here cannot decode.
- User: "it doesnt let you see comments or make them": read-only Comments tab (top 40). Posting is
  left out on purpose (would need YouTube's private API with the browser cookies; ToS risk).
- User: "please make sure that this doesnt get me in trouble": renamed OrinTube (was "YouTube
  Viewer" with a red "▶ YouTube" logo), own icon and accent (amber, then NVIDIA-style green #76b900 at the user's request), README legal/privacy section,
  MIT license, the user's name removed from code and history.
- Verified headless: a live stream (CNN, 720p) and a Short (NASA, 1080x1920) decode on NVDEC.
  On screen: Shorts grid, a Short playing in its vertical frame, its comments.
- Measured for the README (Big Buck Bunny 1080p60): browser (AV1, software) about 2.3 cores, whole
  system 45% busy, 0 dropped; OrinTube 0.5 of one core, system 26% busy, 60 fps. Decode only:
  H.264 NVDEC 42% vs FFmpeg 122% of one core; VP9 NVDEC 30% vs FFmpeg 114%.
- Known risk: yt-dlp has deprecated Python 3.10 (JetPack 6's Python).

- Icons (user: "orin tube lost the icon ... its just a gear", "i want a real icon in the start
  menu as well"): new tile icon (screen, play button, progress bar), PNG renders 16-512 px plus
  the SVG, launcher points at the 256 px PNG by full path (GNOME had kept a cached gear).

## 2026-10-21: original audio instead of auto-dubbed tracks
- User: some Shorts from History "come out in Japanese, even though they played in english on my phone".
- Cause: YouTube's auto-dubbed audio tracks. One Short had 42 HLS audio tracks (40 "dubbed-auto",
  2 "American English - original"); format ids were compared as text, so "234-9" (Japanese) beat
  "234-20" (original). Now the original track wins, then English, then the higher bitrate,
  in both the signed-out and the signed-in (muxed, 132 formats in 22 languages) paths.
- Checked on that Short: both paths now pick acont=original, lang=en-US. 26 unit tests pass.

## 2026-10-22: paused frame stays on screen
- User: "when i ever i pause a video, the screen goes black instead of freezing the image".
- Cause: any redraw while paused painted the video area black (the draw handler did), and
  nv3dsink does not repaint a paused frame (with or without enable-last-sample).
  Test: brightness 235 paused -> 0 after a redraw.
- Fix: the video area is painted black only until the first frame arrives (window
  background None, app-paintable), so a redraw keeps the frame (235 -> 235). For a resize or
  fullscreen while paused, the player copies the paused frame from the screen 150 ms after
  pausing and paints it scaled. Re-showing it with a flushing seek while paused was tried
  and hung the player, so it is not used.
- pause_app.py: paused, fullscreen, back and resume all keep the picture; player_check.py 7/7.

## Known limits
- Videos that need the signed-in fallback take 12-16 s to start; after 4 s the app says so.
- If both tries fail, the watch page offers "Watch in Chromium".
- Seeks land 0-6 s before the target (start of YouTube's ~5 s HLS segment).
- H.264 only, up to 1080p (the screen is 1360x768).
- Not verified: live streams in the app (the format selection was checked with a CNN-News18 live
  stream, but it was never played), and Shorts' 9:16 frame on screen.
