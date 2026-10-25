"""YouTube data for the viewer: personal feeds, search, and the stream URLs to play.

Where it sits: ui.py calls this module from background threads. It hands back plain
VideoItem lists for the grids and a Streams object for player.py. All network access
goes through the yt_dlp Python library (pip --user, 2026.08.19 when written).

Sign-in:
    yt-dlp cannot sign in with a password. It reuses the YouTube session of a browser on
    this machine by reading its cookies (read-only). find_browser() picks the first of
    KNOWN_BROWSERS whose profile exists: the Chromium snap (Ubuntu's default, used on the
    Jetson where this was written), a .deb Chromium, then Google Chrome.
    2026-10-19 (the user chose "Yes, use Chromium's sign-in"). Test: cookies load in about
    3 s, and Home (:ytrec), Subscriptions (:ytsubs), History (:ythistory) and Watch later
    (:ytwatchlater) each return the account's own videos in 1-2 s.
    Signing out of YouTube in that browser signs the viewer out too.

Streams (two tries):
    1. Without the sign-in cookies (about 2 s): separate H.264 video and AAC audio HLS playlists.
    2. If that fails or offers no HLS: with the sign-in and YouTube's "web_safari" client
       (about 8 s): one HLS playlist carrying H.264 video and AAC audio together ("muxed").
    2026-10-19 (user: "yes" to installing deno 2.9.7 + yt-dlp-ejs 0.8.0, after a SpongeBob
    compilation from Home failed with "This video is not available"): with the JS runtime
    the SpongeBob video resolves, but signed out it only offered DASH, which the player
    cannot read (HTTP 403 without &range= chunks). Signed in, web_safari offered muxed HLS
    (itags 91-96, 300-301) for SpongeBob, Big Buck Bunny and an NBA game alike.
    Earlier note, still true for try 1:
    Stream URLs are fetched WITHOUT the sign-in cookies. 2026-10-19 test: with cookies,
    yt-dlp switches to clients that need a JavaScript runtime (none is installed) and
    returns "Requested format is not available" for every video. Anonymous extraction
    lists HLS playlists for public videos and live streams. So members-only and
    age-restricted videos cannot play until a JS runtime (deno) is installed.
    Chosen formats: the tallest H.264 (avc1) video-only HLS playlist at or below the
    height limit (pixels), plus the "Default, high" audio-only HLS playlist (itag 234).

Shorts:
    2026-10-19 (user: "no youtube shorts either"): the Home and Subscriptions feeds contain no
    Shorts (0 of 60 each), and YouTube's Shorts search filter returned 7 results once and 0
    the next time. load_shorts() instead takes the channels from the Subscriptions feed
    (26 channels) and reads each channel's Shorts tab, SHORTS_CHANNELS_PER_PAGE at a time in
    parallel (12 tabs in 3.2 s, 5 newest Shorts each), mixing the channels together.

Comments (read-only):
    2026-10-19 (user: "it doesnt let you see comments or make them"): get_comments() reads the
    top COMMENT_COUNT comments with yt-dlp (2-3 s, signed out). Posting is deliberately left
    out: it would mean driving YouTube's private web API with the browser's cookies, which
    YouTube's terms do not allow and which can get an account flagged. The UI sends people
    to the video's page in their browser to comment instead.

Related videos (Up next):
    2026-10-19 (user: "the up next section is missing"): a video opened from a link had an
    empty Up next, because Up next only showed the rest of the list it was clicked in.
    yt-dlp does not return YouTube's related videos, but the watch page embeds them in its
    ytInitialData JSON as "lockupViewModel" items (20 for a SpongeBob compilation, about
    0.7 s to fetch). get_related() reads those; parse_related_videos() does the parsing.

What can go wrong:
    - YouTube changes the watch page layout: get_related() then returns an empty list,
      and the UI falls back to the list the video was opened from.
    - The browser's cookie database is locked or its key is unavailable (keyring locked):
      feeds raise SignInError and the UI falls back to search only.
    - YouTube changes break yt-dlp: errors surface as yt_dlp DownloadError text.
      Fix: pip install --user -U yt-dlp
"""

import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import yt_dlp

from .log import log



@dataclass(frozen=True)
class Browser:
    """A browser whose YouTube sign-in can be reused.

    name: shown to the user. yt_dlp_name: yt-dlp's name for its cookie format.
    profile_dir: its profile folder (with ~). command: the program that opens a URL in it.
    """
    name: str
    yt_dlp_name: str
    profile_dir: str
    command: str


KNOWN_BROWSERS = [
    Browser("Chromium", "chromium", "~/snap/chromium/common/chromium", "chromium"),
    Browser("Chromium", "chromium", "~/.config/chromium", "chromium"),
    Browser("Google Chrome", "chrome", "~/.config/google-chrome", "google-chrome"),
]


def find_browser(home_dir=None):
    """Return the first KNOWN_BROWSERS entry whose profile folder exists, or the first entry if none does.

    home_dir: the home folder to look in (str); None means the current user's. With no
    browser found, sign-in simply fails later with SignInError ("not signed in").
    """
    for browser in KNOWN_BROWSERS:
        profile_dir = browser.profile_dir.replace("~", home_dir, 1) if home_dir else os.path.expanduser(browser.profile_dir)
        if os.path.isdir(profile_dir):
            return browser
    return KNOWN_BROWSERS[0]

# Feed name (used by the UI) -> yt-dlp's feed URL.
FEED_URLS = {
    "home": ":ytrec",
    "subscriptions": ":ytsubs",
    "history": ":ythistory",
    "watch_later": ":ytwatchlater",
}

FEED_PAGE_SIZE = 24
COMMENT_COUNT = 40
SHORTS_CHANNELS_PER_PAGE = 12
SHORTS_PER_CHANNEL = 5
SHORTS_TAB_URL = "https://www.youtube.com/channel/{channel_id}/shorts"
SHORTS_CHANNELS_MAX_AGE_S = 60 * 60  # re-read which channels to show Shorts from, hourly
THUMBNAIL_URL = "https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
# 2026-10-19: the portrait URL that Shorts tabs list is WebP, and GdkPixbuf here has no WebP
# loader (blank Shorts cards). oar2.jpg is the same cover as a 1080x1920 JPEG.
SHORT_THUMBNAIL_URL = "https://i.ytimg.com/vi/{video_id}/oar2.jpg"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

# Cookies proving a signed-in session. LOGIN_INFO alone can be a leftover after signing out.
SIGNED_IN_COOKIE_NAMES = {"SID", "SAPISID"}

# Matches the video id in the links people paste: watch?v=, youtu.be/, /shorts/, /live/.
# The start time in a pasted link: t=90, t=90s, t=1m30s, t=1h2m3s, or start=90.
START_TIME_PATTERN = re.compile(r"[?&#](?:t|start)=(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?(?:&|$)")

VIDEO_ID_PATTERN = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


class SignInError(Exception):
    """The browser's YouTube session could not be read (no cookies, locked keyring, or signed out)."""


@dataclass
class VideoItem:
    """One video as shown in a grid or the Up next list.

    duration_s: length in seconds, or None for live streams and premieres.
    view_count: number of views, or None when YouTube did not say.
    """
    video_id: str
    title: str
    channel: str
    duration_s: int = None
    view_count: int = None
    is_short: bool = False
    portrait_thumbnail_url: str = None

    @property
    def url(self):
        """The watch page URL (str)."""
        return WATCH_URL.format(video_id=self.video_id)

    @property
    def thumbnail_url(self):
        """URL of the thumbnail: the 9:16 portrait cover for Shorts, else the 320x180 JPEG."""
        return self.portrait_thumbnail_url or THUMBNAIL_URL.format(video_id=self.video_id)


@dataclass
class Comment:
    """One top-level comment. like_count: int or None. time_text: YouTube's "2 years ago" text."""
    author: str
    text: str
    like_count: int = None
    time_text: str = ""
    is_pinned: bool = False
    is_by_uploader: bool = False


@dataclass
class Streams:
    """What player.py needs to play one video.

    video_url: HLS playlist URL (str). audio_url: the separate audio HLS playlist (str), or
        None when the audio is inside the video stream ("muxed"). height_px: the chosen video height.
    aspect: picture width / height (float; 1.78 for 16:9, 0.56 for a vertical Short).
    duration_s: None for live streams.
    """
    video_id: str
    title: str
    channel: str
    duration_s: int
    is_live: bool
    video_url: str
    audio_url: str
    height_px: int
    fps: float
    aspect: float


def extract_video_id(text):
    """Return the 11-character video id from a pasted YouTube link (str), or None if it is not one."""
    match = VIDEO_ID_PATTERN.search(text.strip())
    return match.group(1) if match else None


def extract_start_time_s(text):
    """Return the start time in seconds from a pasted link's t= / start= parameter (int), or None."""
    match = START_TIME_PATTERN.search(text.strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(part) if part else 0 for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def comment_from_entry(entry):
    """Turn one of yt-dlp's comment dicts into a Comment, or None for replies and empty comments."""
    if entry.get("parent", "root") != "root" or not entry.get("text"):
        return None
    return Comment(
        author=entry.get("author") or "",
        text=entry["text"].strip(),
        like_count=entry.get("like_count"),
        time_text=entry.get("_time_text") or "",
        is_pinned=bool(entry.get("is_pinned")),
        is_by_uploader=bool(entry.get("author_is_uploader")),
    )


def entry_to_video_item(entry):
    """Turn one yt-dlp flat playlist entry (dict) into a VideoItem, or None when it has no video id."""
    video_id = entry.get("id")
    if not video_id or len(video_id) != 11:
        return None
    duration = entry.get("duration")
    is_short = "/shorts/" in (entry.get("url") or "")
    return VideoItem(
        video_id=video_id,
        title=entry.get("title") or video_id,
        channel=entry.get("channel") or entry.get("uploader") or "",
        duration_s=int(duration) if duration else None,
        view_count=entry.get("view_count"),
        is_short=is_short,
        portrait_thumbnail_url=SHORT_THUMBNAIL_URL.format(video_id=video_id) if is_short else None,
    )


WATCH_PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
WATCH_PAGE_TIMEOUT_S = 15
INITIAL_DATA_PATTERN = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.DOTALL)
SHORT_COUNT_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_short_count(text):
    """Turn YouTube's short counts ("20M", "1.1M", "956K", "835", "1.2M views") into an int, or None."""
    match = re.match(r"^\s*([\d.,]+)\s*([KMB])?", text or "")
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    return int(number * SHORT_COUNT_SUFFIXES.get(match.group(2), 1))


def parse_clock_duration(text):
    """Turn "4:02:01" / "23:18" into seconds (int). Anything else ("LIVE", "SHORTS") gives None."""
    if not re.fullmatch(r"\d+(:\d{1,2}){1,2}", (text or "").strip()):
        return None
    seconds = 0
    for part in text.strip().split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def _iter_lockups(node):
    """Yield every "lockupViewModel" dict anywhere inside a parsed ytInitialData structure."""
    if isinstance(node, dict):
        if "lockupViewModel" in node:
            yield node["lockupViewModel"]
        for value in node.values():
            yield from _iter_lockups(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_lockups(value)


def lockup_to_video_item(lockup):
    """Turn one lockupViewModel (dict) into a VideoItem, or None for playlists, channels and broken items."""
    if lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None
    try:
        metadata = lockup["metadata"]["lockupMetadataViewModel"]
        title = metadata["title"]["content"]
        rows = metadata.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
        row_texts = [[part.get("text", {}).get("content", "") for part in row.get("metadataParts", [])] for row in rows]
        overlays = lockup.get("contentImage", {}).get("thumbnailViewModel", {}).get("overlays", [])
    except (KeyError, TypeError):
        return None
    badge_texts = [
        badge.get("thumbnailBadgeViewModel", {}).get("text", "")
        for overlay in overlays
        for badge in overlay.get("thumbnailBottomOverlayViewModel", {}).get("badges", [])
    ]
    return VideoItem(
        video_id=lockup.get("contentId", ""),
        title=title,
        channel=row_texts[0][0] if row_texts and row_texts[0] else "",
        duration_s=next((parse_clock_duration(text) for text in badge_texts if parse_clock_duration(text)), None),
        view_count=parse_short_count(row_texts[1][0]) if len(row_texts) > 1 and row_texts[1] else None,
    )


def parse_related_videos(initial_data, exclude_video_id=None):
    """Return the related VideoItems from a watch page's ytInitialData (dict), in page order.

    Only the right-hand "secondaryResults" column is read; the video itself is left out.
    """
    try:
        column = initial_data["contents"]["twoColumnWatchNextResults"]["secondaryResults"]
    except (KeyError, TypeError):
        return []
    items, seen = [], {exclude_video_id}
    for lockup in _iter_lockups(column):
        item = lockup_to_video_item(lockup)
        if item is not None and len(item.video_id) == 11 and item.video_id not in seen:
            seen.add(item.video_id)
            items.append(item)
    return items


def quality_px(video_format):
    """Return a format's quality in YouTube's sense: its shorter side in pixels (0 when unknown).

    2026-10-19: a vertical Short at "1080p" is 1080x1920, so comparing the height with the
    1080 limit capped Shorts at 854p (NASA Short -62C9R_41Sc came out 480x854). YouTube names
    qualities after the shorter side, and so does this.
    """
    width, height = video_format.get("width"), video_format.get("height")
    if width and height:
        return min(width, height)
    return height or 0


def audio_language_rank(audio_format):
    """Rank an audio track (yt-dlp format dict): higher is better. (is original, is English).

    2026-10-21 (user: Shorts "come out in Japanese, even though they played in english on my
    phone"): many videos now carry YouTube's auto-dubbed tracks. A Short from History had 42
    HLS audio tracks: 40 "dubbed-auto" ones and 2 marked "American English - original
    (original)" with language_preference 10. Comparing format ids as text picked "234-9"
    (Japanese) over "234-20" (the original). The original track always comes first now.
    """
    note = str(audio_format.get("format_note") or "").lower()
    is_original = (audio_format.get("language_preference") or 0) > 0 or "original" in note
    is_english = str(audio_format.get("language") or "").lower().startswith("en")
    return is_original, is_english


def choose_muxed_hls_format(formats, max_height_px):
    """Pick the tallest HLS format with H.264 video AND AAC audio together, at or below max_height_px.

    Returns a yt-dlp format dict. Raises LookupError when there is none.
    """
    muxed = [
        f for f in formats
        if str(f.get("protocol", "")).startswith("m3u8")
        and str(f.get("vcodec") or "").startswith("avc1")
        and str(f.get("acodec") or "").startswith("mp4a")
        and quality_px(f) <= max_height_px
    ]
    if not muxed:
        raise LookupError(f"no H.264 stream at or below {max_height_px}p for this video")
    return max(muxed, key=lambda f: (quality_px(f), f.get("fps") or 0, audio_language_rank(f), f.get("tbr") or 0))


def choose_hls_formats(formats, max_height_px):
    """Pick the HLS video and audio formats to play from yt-dlp's `formats` list.

    max_height_px: the highest quality allowed, as the video's shorter side in pixels (see quality_px).
    Returns (video_format, audio_format), both yt-dlp format dicts.
    Raises LookupError when there is no H.264 HLS video at or below the limit, or no HLS audio.
    """
    hls = [f for f in formats if str(f.get("protocol", "")).startswith("m3u8")]
    videos = [
        f for f in hls
        if str(f.get("vcodec") or "").startswith("avc1") and quality_px(f) <= max_height_px
    ]
    # Audio-only HLS formats have vcodec "none"; their acodec is left empty by yt-dlp.
    audios = [f for f in hls if f.get("vcodec") == "none"]
    if not videos:
        raise LookupError(f"no H.264 stream at or below {max_height_px}p for this video")
    if not audios:
        raise LookupError("no audio stream for this video")
    best_video = max(videos, key=lambda f: (quality_px(f), f.get("fps") or 0, f.get("tbr") or 0))
    # The original-language track first (see audio_language_rank), then English, then
    # "Default, high" (itag 234) over "Default, low" (233); neither has a bitrate in the list.
    best_audio = max(audios, key=lambda f: (audio_language_rank(f), _is_high_quality_audio(f)))
    return best_video, best_audio


class YouTube:
    """Loads feeds, searches and resolves streams. Safe to call from several threads.

    use_sign_in: read the browser's YouTube session for the personal feeds (bool).
    browser: the Browser to read it from; None means find_browser().
    """

    def __init__(self, use_sign_in=True, browser=None):
        self.use_sign_in = use_sign_in
        self.browser = browser or find_browser()
        self._signed_in_client = None
        self._signed_in_lock = threading.Lock()
        self._signed_in_stream_client = None
        self._signed_in_stream_lock = threading.Lock()
        self._shorts_channels = None
        self._shorts_channels_read_at = 0.0

    # Signed-in feeds

    def _get_signed_in_client(self):
        """Return the YoutubeDL that carries the browser's cookies, creating it on first use.

        Loading the cookies takes about 3 s, so it happens once per app run.
        Raises SignInError when there is no usable signed-in session.
        Callers must hold self._signed_in_lock (YoutubeDL objects are not thread-safe).
        """
        if self._signed_in_client is not None:
            return self._signed_in_client
        if not self.use_sign_in:
            raise SignInError("sign-in is turned off")
        options = dict(
            _flat_options(),
            cookiesfrombrowser=self._cookie_source(),
        )
        try:
            client = yt_dlp.YoutubeDL(options)
            cookie_names = {cookie.name for cookie in client.cookiejar if "youtube.com" in cookie.domain}
        except Exception as error:  # yt-dlp raises plain Exceptions for keyring / database problems
            raise SignInError(f"could not read the browser's YouTube sign-in: {error}") from error
        if not SIGNED_IN_COOKIE_NAMES <= cookie_names:
            raise SignInError(f"not signed in to YouTube in {self.browser.name}")
        self._signed_in_client = client
        return client

    def _cookie_source(self):
        """yt-dlp's cookiesfrombrowser value for self.browser: (name, profile folder, keyring, container)."""
        return (self.browser.yt_dlp_name, os.path.expanduser(self.browser.profile_dir), None, None)

    def forget_sign_in(self):
        """Drop the cookies read earlier, so the next feed request reads the browser's session again."""
        with self._signed_in_lock:
            self._signed_in_client = None
        with self._signed_in_stream_lock:
            self._signed_in_stream_client = None

    def is_signed_in(self):
        """Return True when the browser's YouTube session is usable (loads the cookies on first call)."""
        try:
            with self._signed_in_lock:
                self._get_signed_in_client()
            return True
        except SignInError:
            return False

    def load_feed(self, feed_name, start_index=0, count=FEED_PAGE_SIZE):
        """Return up to `count` VideoItems from a personal feed, skipping the first `start_index`.

        feed_name: a key of FEED_URLS ("home", "subscriptions", "history", "watch_later").
        Returns (items, has_more): has_more is False once YouTube returned less than a full page.
        Raises SignInError when not signed in, or yt_dlp's DownloadError on network/YouTube failure.
        """
        with self._signed_in_lock:
            client = self._get_signed_in_client()
            client.params["playliststart"] = start_index + 1
            client.params["playlistend"] = start_index + count
            info = client.extract_info(FEED_URLS[feed_name], download=False)
        return _entries_to_page(info, count)

    def load_shorts(self, page_index):
        """Return (Shorts, has_more) from page `page_index` (0, 1, ...) of the subscribed channels.

        Each page reads SHORTS_CHANNELS_PER_PAGE channels' Shorts tabs in parallel (about 3 s)
        and mixes them: the newest Short of each channel first, then the second, and so on.
        Raises SignInError when not signed in.
        """
        channels = self._get_shorts_channels()
        first = page_index * SHORTS_CHANNELS_PER_PAGE
        batch = channels[first:first + SHORTS_CHANNELS_PER_PAGE]
        with ThreadPoolExecutor(max_workers=len(batch) or 1) as pool:
            per_channel = list(pool.map(lambda channel: _read_shorts_tab(*channel), batch))
        mixed = [shorts[rank] for rank in range(SHORTS_PER_CHANNEL) for shorts in per_channel if rank < len(shorts)]
        return mixed, first + SHORTS_CHANNELS_PER_PAGE < len(channels)

    def _get_shorts_channels(self):
        """The (channel id, channel name) pairs seen in the Subscriptions feed, in feed order (cached)."""
        if self._shorts_channels is None or time.monotonic() - self._shorts_channels_read_at > SHORTS_CHANNELS_MAX_AGE_S:
            with self._signed_in_lock:
                client = self._get_signed_in_client()
                client.params["playliststart"] = 1
                client.params["playlistend"] = 100
                info = client.extract_info(FEED_URLS["subscriptions"], download=False)
            channels = {}
            for entry in info.get("entries") or []:
                channel_id = (entry or {}).get("channel_id")
                if channel_id and channel_id not in channels:
                    channels[channel_id] = entry.get("channel") or entry.get("uploader") or ""
            self._shorts_channels = list(channels.items())
            self._shorts_channels_read_at = time.monotonic()
        return self._shorts_channels

    # Anonymous: search and streams

    def search(self, query, start_index=0, count=FEED_PAGE_SIZE):
        """Return up to `count` VideoItems for search words (str), skipping the first `start_index`.

        Searches anonymously, so results are the same for everyone.
        Returns (items, has_more) like load_feed().
        Raises yt_dlp's DownloadError on network/YouTube failure.
        """
        options = dict(_flat_options(), playliststart=start_index + 1)
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(f"ytsearch{start_index + count}:{query}", download=False)
        return _entries_to_page(info, count)

    def get_streams(self, video_id, max_height_px, still_wanted=lambda: True):
        """Resolve the HLS playlists for one video (str id) at or below max_height_px (pixels).

        Tries signed out first, then signed in with the web_safari client (see the module docstring).
        still_wanted(): checked before the slow signed-in try. 2026-10-19 review: clicking
        through several such videos queued an ~8 s lookup per click, so the last one waited ~30 s.
        Returns a Streams. Raises LookupError or yt_dlp's DownloadError when both tries fail;
        the error from the signed-in try is raised, since it is the one with more access.
        """
        try:
            streams = self._get_separate_streams(video_id, max_height_px)
            if streams.is_live and self.use_sign_in and still_wanted():
                return self._prefer_single_playlist_for_live(streams, max_height_px)
            return streams
        except Exception as signed_out_error:  # LookupError (no HLS) or any yt-dlp error
            log(f"{video_id}: signed-out lookup failed: {str(signed_out_error)[:200]}")
            if not self.use_sign_in or not still_wanted():
                raise
            try:
                return self._get_muxed_streams(video_id, max_height_px)
            except SignInError:
                raise signed_out_error

    def get_comments(self, video_id, count=COMMENT_COUNT):
        """Return the top `count` comments of a video (list of Comment), most liked first.

        About 2-3 s, signed out, no replies. Raises yt_dlp's DownloadError on failure
        (for example when comments are turned off).
        """
        options = {
            "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "getcomments": True,
            # max_comments: total, top-level, replies in total, replies per thread.
            "extractor_args": {"youtube": {"max_comments": [str(count), str(count), "0", "0"],
                                           "comment_sort": ["top"]}},
        }
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(WATCH_URL.format(video_id=video_id), download=False)
        comments = [comment_from_entry(entry) for entry in info.get("comments") or []]
        return [comment for comment in comments if comment is not None]

    def get_related(self, video_id):
        """Return YouTube's related videos for `video_id` (list of VideoItem), as its Up next shows them.

        Reads the public watch page (about 0.7 s, no sign-in). Returns [] when the page
        cannot be fetched or its layout is not recognised.
        """
        request = urllib.request.Request(WATCH_URL.format(video_id=video_id) + "&hl=en&gl=US", headers=WATCH_PAGE_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=WATCH_PAGE_TIMEOUT_S) as response:
                page = response.read().decode("utf-8", "replace")
        except OSError:
            return []
        match = INITIAL_DATA_PATTERN.search(page)
        if not match:
            return []
        try:
            initial_data = json.loads(match.group(1))
        except ValueError:
            return []
        return parse_related_videos(initial_data, exclude_video_id=video_id)

    def _prefer_single_playlist_for_live(self, separate_streams, max_height_px):
        """For a live stream, return the signed-in playlist that carries video and audio together, if there is one.

        2026-10-25 (user, on live streams: the sound "gets there then stays about a half or quarter
        second behind"; Chromium and VLC stay in sync on the same TV): with separate video and
        audio playlists, each hlsdemux joins the live stream on its own and starts its own
        timeline, so the two can settle a fraction of a second apart for the whole session (VOD
        both start at 0:00, which is why the drift checks on Big Buck Bunny never showed it).
        A single MPEG-TS playlist carries both with their real PTS, so they cannot slip.
        Falls back to the separate playlists when the signed-in lookup fails.
        """
        try:
            return self._get_muxed_streams(separate_streams.video_id, max_height_px)
        except Exception as error:  # sign-in or YouTube problems: the separate playlists still play
            log(f"{separate_streams.video_id}: live single-playlist lookup failed, using separate playlists: {str(error)[:200]}")
            return separate_streams

    def _get_separate_streams(self, video_id, max_height_px):
        """Try 1, signed out: separate video and audio HLS playlists. About 2 s."""
        options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(WATCH_URL.format(video_id=video_id), download=False)
        video_format, audio_format = choose_hls_formats(info.get("formats") or [], max_height_px)
        return _make_streams(video_id, info, video_format, audio_format["url"])

    def _get_muxed_streams(self, video_id, max_height_px):
        """Try 2, signed in, web_safari client: one HLS playlist with video and audio. About 8 s.

        Raises SignInError when the browser's session cannot be read.
        """
        with self._signed_in_stream_lock:
            if self._signed_in_stream_client is None:
                options = {
                    "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                    "cookiesfrombrowser": self._cookie_source(),
                    "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
                }
                try:
                    client = yt_dlp.YoutubeDL(options)
                    # yt-dlp reads the cookies lazily; touching the jar makes a locked keyring
                    # or missing profile fail here, as a SignInError (2026-10-19 review).
                    len(client.cookiejar)
                    self._signed_in_stream_client = client
                except Exception as error:  # keyring / cookie database problems
                    raise SignInError(f"could not read the browser's YouTube sign-in: {error}") from error
            info = self._signed_in_stream_client.extract_info(WATCH_URL.format(video_id=video_id), download=False)
        muxed_format = choose_muxed_hls_format(info.get("formats") or [], max_height_px)
        return _make_streams(video_id, info, muxed_format, None)


def _make_streams(video_id, info, video_format, audio_url):
    """Build a Streams from yt-dlp's video info (dict), the chosen video format and the audio URL (or None)."""
    duration = info.get("duration")
    return Streams(
        video_id=video_id,
        title=info.get("title") or video_id,
        channel=info.get("channel") or info.get("uploader") or "",
        duration_s=int(duration) if duration else None,
        is_live=bool(info.get("is_live")),
        video_url=video_format["url"],
        audio_url=audio_url,
        height_px=quality_px(video_format),
        fps=video_format.get("fps") or 0,
        aspect=_aspect_of(video_format),
    )


def _is_high_quality_audio(audio_format):
    """True for YouTube's higher-bitrate HLS audio: itag 234 ("Default, high"), also as "234-<n>"."""
    return audio_format.get("format_id", "").split("-")[0] == "234" or "high" in str(audio_format.get("format_note", "")).lower()


def _aspect_of(video_format):
    """Return a format's width / height (float), or 16/9 when yt-dlp did not report its size."""
    width, height = video_format.get("width"), video_format.get("height")
    if width and height:
        return width / height
    return 16 / 9


def _flat_options():
    """yt-dlp options for listing videos without opening each one (about 1-2 s per page)."""
    return {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"}


def _read_shorts_tab(channel_id, channel_name):
    """Return the newest SHORTS_PER_CHANNEL Shorts of one channel (list of VideoItem); [] on any error.

    channel_name: filled into each item, because Shorts tab entries do not carry it.
    """
    options = dict(_flat_options(), playlistend=SHORTS_PER_CHANNEL)
    try:
        with yt_dlp.YoutubeDL(options) as client:
            info = client.extract_info(SHORTS_TAB_URL.format(channel_id=channel_id), download=False)
    except Exception:  # channel without a Shorts tab, removed channel, network: skip that channel
        return []
    items, _has_more = _entries_to_page(info, SHORTS_PER_CHANNEL)
    for item in items:
        item.channel = item.channel or channel_name
    return [item for item in items if item.is_short]


def _entries_to_page(info, page_size):
    """Turn a yt-dlp playlist result (dict) into (VideoItems, has_more), skipping channels and playlists.

    has_more counts every entry YouTube returned, channels included. 2026-10-19 review:
    counting only the videos stopped "load more" after any page that contained a channel.
    """
    entries = (info or {}).get("entries") or []
    items = []
    for entry in entries:
        item = entry_to_video_item(entry or {})
        if item is not None:
            items.append(item)
    return items, len(entries) >= page_size
