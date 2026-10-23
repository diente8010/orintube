"""The OrinTube window: a YouTube-style browse grid and a watch page with the hardware player.

Where it sits: __main__.py creates one ViewerWindow. It uses youtube.py for feeds,
search and stream URLs (always from background threads), thumbnails.py for pictures,
and player.py to play inside the window on the Jetson's hardware decoder.

Layout:
    Header bar: back arrow (watch page only), OrinTube logo, search box, account menu.
    Browse page: sidebar (Home, Subscriptions, History, Watch later) + grid of video
        cards. Scrolling to the bottom loads the next FEED_PAGE_SIZE videos.
    Watch page: video, control bar, title/channel, and "Up next": YouTube's related videos
        (until they load, or if they cannot, the rest of the list the video was opened
        from). When a video ends, the first Up next video starts (autoplay, can be
        switched off in the Up next header).

Keyboard (watch page, while the search box does not have focus):
    Space or K  pause / play          F           fullscreen on / off
    Left / J    back 5 s / 10 s       Right / L   forward 5 s / 10 s
    Up / Down   volume +/- 5%         M           mute
    Shift+N     next video            Shift+P     previous video
    0-9         jump to 0%-90%        Home        back to the start
    [ / ]       sound 50 ms earlier / later (saved; for TVs that delay the picture)
    Escape      leave fullscreen, then back to browse
    /           jump to the search box
    F5, Ctrl+R  refresh the list (also automatic when coming Back to a list older than 10 min)
Mouse: click the video to pause, double-click for fullscreen. In fullscreen, the
control bar and pointer hide after CONTROLS_HIDE_DELAY_S seconds without movement.

What can go wrong:
    - Not signed in to YouTube in the browser: the personal sections are greyed out, and
      the account menu offers to open the browser's sign-in page.
    - Errors from YouTube or the player appear in red under the video or above the grid.
"""

import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .player import VideoPlayer  # noqa: E402
from .settings import load_settings, save_settings  # noqa: E402
from .thumbnails import ThumbnailLoader  # noqa: E402
from .youtube import SignInError, VideoItem, YouTube, extract_start_time_s, extract_video_id  # noqa: E402

WINDOW_WIDTH_PX = 1280
WINDOW_HEIGHT_PX = 720
SIDEBAR_WIDTH_PX = 200
UP_NEXT_WIDTH_PX = 360

CARD_THUMBNAIL_WIDTH_PX = 304
CARD_THUMBNAIL_HEIGHT_PX = 171
UP_NEXT_THUMBNAIL_WIDTH_PX = 160
UP_NEXT_THUMBNAIL_HEIGHT_PX = 90
SHORT_CARD_HEIGHT_PX = 260
SHORT_THUMBNAIL_ASPECT = 9 / 16  # oar2.jpg covers are 1080x1920
COMMENT_MAX_LINES = 12

QUALITY_CHOICES_PX = ["1080", "720", "480", "360"]
DEFAULT_QUALITY_PX = "1080"
SEEK_SMALL_S = 5
SEEK_LARGE_S = 10
VOLUME_STEP = 0.05
SEEK_BAR_DEBOUNCE_MS = 200
AUDIO_DELAY_STEP_MS = 50
AUDIO_DELAY_LIMIT_MS = 3000
NOTICE_DURATION_S = 2
# 2026-10-22 (user: "the player isnt giving me recent results"): a section was loaded once and
# then kept; going Back showed the same grid, and a window open for days showed days-old lists
# (YouTube itself returned the previous night's games). Now Back reloads a list older than this.
STALE_LISTING_S = 10 * 60
CONTROLS_HIDE_DELAY_S = 3
SLOW_LOAD_NOTICE_S = 4
# Load more videos when the grid is scrolled to within this many pixels of the bottom.
LOAD_MORE_MARGIN_PX = 400

YOUTUBE_SIGN_IN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/"

SECTIONS = [
    # (section id, sidebar label, icon name, needs sign-in)
    ("home", "Home", "go-home-symbolic", True),
    ("subscriptions", "Subscriptions", "view-list-symbolic", True),
    ("history", "History", "document-open-recent-symbolic", True),
    ("watch_later", "Watch later", "alarm-symbolic", True),
    ("shorts", "Shorts", "camera-video-symbolic", True),
]

# 2026-10-19 (user: "i want the same GUI as the youtube app"): a dark theme laid out like
# YouTube's: page #0f0f0f, raised surfaces #272727, text #f1f1f1, secondary text #aaaaaa.
# The accent is green #76b900 (logo, seek bar), not YouTube's red, so the app is not
# mistaken for an official YouTube product (renamed OrinTube the same day; user: "the icon
# should be nvidia green"). Only the colour: NVIDIA's logo and name are not used. LIVE stays red,
# the usual colour for live badges everywhere.
STYLE_SHEET = b"""
window, .browse, .watch, list, flowbox, scrolledwindow, viewport { background-color: #0f0f0f; color: #f1f1f1; }
headerbar { background: #0f0f0f; border: none; box-shadow: none; min-height: 52px; }
.logo-text { font-weight: 800; font-size: 18px; letter-spacing: -0.5px; }
.search entry, entry.search { background: #121212; border: 1px solid #303030; border-radius: 20px; padding: 4px 14px; color: #f1f1f1; }
.sidebar row { padding: 10px 14px; border-radius: 10px; margin: 1px 8px; }
.sidebar row:selected { background: #272727; color: #f1f1f1; font-weight: bold; }
.sidebar row:hover { background: #272727; }
.section-title { font-size: 20px; font-weight: bold; margin: 16px 24px 4px 24px; }
flowboxchild { padding: 0; border-radius: 12px; }
flowboxchild:hover { background: #1f1f1f; }
.card-title { font-weight: 600; font-size: 14px; }
.secondary { color: #aaaaaa; font-size: 12px; }
.duration-badge { background: rgba(0, 0, 0, 0.8); color: white; font-size: 11px; font-weight: bold;
                  border-radius: 4px; padding: 1px 4px; margin: 6px; }
.live-badge { background: #cc0000; }
.video-background, .video-background frame { background-color: #000000; border: none; }
.controls { background: #0f0f0f; padding: 2px 8px; }
.controls button { background: none; border: none; box-shadow: none; color: #f1f1f1; }
.seek-bar trough { min-height: 4px; background: rgba(255, 255, 255, 0.25); }
.seek-bar highlight { background: #76b900; }
.seek-bar slider { background: #a4e44a; min-width: 12px; min-height: 12px; border: none; }
.watch-title { font-size: 18px; font-weight: bold; }
.error { color: #ff6b6b; }
.up-next-header { font-weight: bold; font-size: 16px; }
.up-next row { padding: 4px; border-radius: 8px; }
.comment-author { font-weight: bold; font-size: 12px; }
.comment-text { font-size: 13px; }
.up-next row:hover { background: #272727; }
"""


def format_duration(seconds):
    """Turn seconds (int or None) into "m:ss" / "h:mm:ss". None (live stream) gives "LIVE"."""
    if seconds is None:
        return "LIVE"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_count(count):
    """Turn a count (int) into YouTube's short form: 2200000 -> "2.2M", 9900 -> "9.9K", 835 -> "835"."""
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if count >= threshold:
            return f"{count / threshold:.1f}".rstrip("0").rstrip(".") + suffix
    return str(count)


def format_views(view_count):
    """Turn a view count (int or None) into "1.2M views", "835 views", or ""."""
    if view_count is None:
        return ""
    return f"{format_count(view_count)} views"


def secondary_line(item):
    """The grey line under a title: "Channel · 1.2M views" (parts YouTube did not give are left out)."""
    return "  ·  ".join(part for part in (item.channel, format_views(item.view_count)) if part)


def make_label(text="", css_class=None, lines=1, xalign=0.0):
    """Create a left-aligned Gtk.Label that wraps to at most `lines` lines and ends with "…" when too long."""
    label = Gtk.Label(label=text, xalign=xalign)
    label.set_line_wrap(lines > 1)
    label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_lines(lines)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    if css_class:
        label.get_style_context().add_class(css_class)
    return label


def thumbnail_size(item, landscape_width_px, landscape_height_px):
    """Return (width, height) in pixels for an item's thumbnail: as given, or 9:16 portrait for a Short."""
    if item.is_short and item.portrait_thumbnail_url:
        return round(landscape_height_px * SHORT_THUMBNAIL_ASPECT), landscape_height_px
    return landscape_width_px, landscape_height_px


def make_thumbnail_with_badge(item, width_px, height_px, thumbnails):
    """Build a thumbnail image with the duration (red LIVE, or "Short") badge in its bottom-right corner."""
    overlay = Gtk.Overlay()
    image = Gtk.Image()
    image.set_size_request(width_px, height_px)
    overlay.add(image)
    if item.is_short:
        badge = make_label("Short", "duration-badge")
    else:
        badge = make_label(format_duration(item.duration_s), "duration-badge")
        if item.duration_s is None:
            badge.get_style_context().add_class("live-badge")
    badge.set_halign(Gtk.Align.END)
    badge.set_valign(Gtk.Align.END)
    overlay.add_overlay(badge)
    thumbnails.load_into(image, item.thumbnail_url, width_px, height_px)
    return overlay


class VideoCard(Gtk.FlowBoxChild):
    """One video in the browse grid: thumbnail with duration, title (2 lines), channel and views."""

    def __init__(self, item, thumbnails):
        super().__init__()
        self.item = item
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)
        # Shorts get tall portrait cards, so a Shorts grid fits more per row (like YouTube's).
        width_px, height_px = thumbnail_size(item, CARD_THUMBNAIL_WIDTH_PX,
                                             SHORT_CARD_HEIGHT_PX if item.is_short else CARD_THUMBNAIL_HEIGHT_PX)
        column.set_size_request(width_px, -1)
        column.pack_start(make_thumbnail_with_badge(item, width_px, height_px, thumbnails), False, False, 0)
        title = make_label(item.title, "card-title", lines=2)
        title.set_max_width_chars(1)  # lets the label wrap to the card width instead of widening it
        title.set_hexpand(True)
        column.pack_start(title, False, False, 0)
        details = make_label(secondary_line(item), "secondary")
        details.set_max_width_chars(1)  # without this the full channel text set the card width (3 Shorts per row)
        details.set_hexpand(True)
        column.pack_start(details, False, False, 0)
        self.add(column)


class UpNextRow(Gtk.ListBoxRow):
    """One video in the Up next column: small thumbnail on the left, title and channel on the right."""

    def __init__(self, item, thumbnails):
        super().__init__()
        self.item = item
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        width_px, height_px = thumbnail_size(item, UP_NEXT_THUMBNAIL_WIDTH_PX, UP_NEXT_THUMBNAIL_HEIGHT_PX)
        thumbnail = make_thumbnail_with_badge(item, width_px, height_px, thumbnails)
        thumbnail.set_size_request(UP_NEXT_THUMBNAIL_WIDTH_PX, -1)  # keeps the titles lined up
        line.pack_start(thumbnail, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = make_label(item.title, "card-title", lines=2)
        title.set_max_width_chars(1)
        title.set_hexpand(True)
        text.pack_start(title, False, False, 0)
        text.pack_start(make_label(secondary_line(item), "secondary"), False, False, 0)
        line.pack_start(text, True, True, 0)
        self.add(line)


class CommentRow(Gtk.ListBoxRow):
    """One comment: author, age and pinned/creator marks, the text, and its likes."""

    def __init__(self, comment):
        super().__init__(activatable=False, selectable=False)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin=6)
        marks = []
        if comment.is_pinned:
            marks.append("Pinned")
        if comment.is_by_uploader:
            marks.append("Creator")
        header = "  ·  ".join(part for part in (comment.author, comment.time_text, *marks) if part)
        column.pack_start(make_label(header, "comment-author"), False, False, 0)
        text = make_label(comment.text, "comment-text", lines=COMMENT_MAX_LINES)
        text.set_max_width_chars(1)  # wrap to the column width
        text.set_hexpand(True)
        text.set_selectable(True)
        column.pack_start(text, False, False, 0)
        if comment.like_count:
            column.pack_start(make_label(f"👍 {format_count(comment.like_count)}", "secondary"), False, False, 0)
        self.add(column)


class ViewerWindow(Gtk.Window):
    """The whole app window. See the module docstring for the layout and the keys.

    youtube: a youtube.YouTube used for every network call.
    """

    def __init__(self, youtube):
        super().__init__(title="OrinTube")
        self.youtube = youtube
        self.thumbnails = ThumbnailLoader()
        self.set_default_size(WINDOW_WIDTH_PX, WINDOW_HEIGHT_PX)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        self._install_style_sheet()

        # Browse state. Each load gets a number, and a result arriving after a newer
        # load has started is thrown away.
        self.section_id = "home"
        self.search_query = None
        self.listed_items = []
        self.browse_generation = 0
        self.listing_loaded_at = time.monotonic()
        self.is_loading_more = False
        self.has_more = True
        self.signed_in = False

        # Watch state.
        self.current_item = None
        self.up_next_items = []
        self.watch_generation = 0
        self.is_fullscreen = False
        self.controls_hide_source_id = None
        self.pending_seek_source_id = None
        self.user_is_dragging_seek_bar = False
        self.resume_position_s = None
        self.watch_history = []

        self._build_header_bar()
        self.pages = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.pages.add_named(self._build_browse_page(), "browse")
        self.pages.add_named(self._build_watch_page(), "watch")
        self.add(self.pages)

        self.player = VideoPlayer(self.video_area, self.on_player_progress, self.on_player_state, self.on_player_finished)
        self.settings = load_settings()
        self.player.set_audio_delay(self.settings["audio_delay_ms"])
        self.notice_until = 0.0  # monotonic seconds; the time label shows a notice until then
        self.connect("key-press-event", self.on_key_press)
        self.connect("window-state-event", self.on_window_state_changed)
        self.connect("destroy", self.on_destroy)

    def start(self, url_to_play=None):
        """Show the window, check the sign-in in the background, then load Home (or play `url_to_play`)."""
        self.show_all()
        self.back_button.hide()
        self.watch_error_label.hide()
        self.open_in_browser_button.hide()
        self.browse_message.hide()
        self.set_browse_message("Checking your YouTube sign-in…")
        threading.Thread(target=self._check_sign_in_in_background, daemon=True).start()
        if url_to_play:
            self.play_pasted_url(url_to_play)

    # Building the window

    def _install_style_sheet(self):
        """Apply STYLE_SHEET (YouTube's dark colours) to the whole app."""
        provider = Gtk.CssProvider()
        provider.load_from_data(STYLE_SHEET)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build_header_bar(self):
        """Header: back arrow, logo, search box in the middle, account menu on the right."""
        self.header = Gtk.HeaderBar(show_close_button=True)
        self.set_titlebar(self.header)

        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self.back_button.set_tooltip_text("Back to videos (Escape)")
        self.back_button.connect("clicked", lambda _button: self.show_browse_page(refresh_if_stale=True))
        self.header.pack_start(self.back_button)

        logo = Gtk.Box(spacing=6)
        logo.pack_start(Gtk.Image.new_from_icon_name("orintube", Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        logo.pack_start(make_label("OrinTube", "logo-text"), False, False, 0)
        logo_button = Gtk.EventBox()
        logo_button.add(logo)
        logo_button.connect("button-press-event", lambda *_args: self.open_section("home"))
        self.header.pack_start(logo_button)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Search, or paste a YouTube link")
        self.search_entry.set_width_chars(50)
        self.search_entry.get_style_context().add_class("search")
        self.search_entry.connect("activate", self.on_search_activated)
        self.header.set_custom_title(self.search_entry)

        self.header.pack_end(self._build_account_menu())
        refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh_button.set_tooltip_text("Refresh (F5)")
        refresh_button.connect("clicked", lambda _button: self.refresh_listing())
        self.header.pack_end(refresh_button)

    def _build_account_menu(self):
        """Account button with a popover: sign-in status, "Sign in with <browser>…", "Refresh sign-in"."""
        self.account_button = Gtk.MenuButton()
        self.account_button.set_image(Gtk.Image.new_from_icon_name("avatar-default-symbolic", Gtk.IconSize.LARGE_TOOLBAR))
        popover = Gtk.Popover()
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.account_status_label = make_label("Checking sign-in…", lines=3)
        self.account_status_label.set_max_width_chars(34)
        column.pack_start(self.account_status_label, False, False, 0)
        sign_in_button = Gtk.ModelButton(text=f"Sign in with {self.youtube.browser.name}…")
        sign_in_button.connect("clicked", lambda _button: self.open_chromium_sign_in())
        refresh_button = Gtk.ModelButton(text="Refresh sign-in")
        refresh_button.connect("clicked", lambda _button: self.refresh_sign_in())
        column.pack_start(sign_in_button, False, False, 0)
        column.pack_start(refresh_button, False, False, 0)
        column.show_all()
        popover.add(column)
        self.account_button.set_popover(popover)
        return self.account_button

    def _build_browse_page(self):
        """Browse page: sidebar on the left, scrolling grid of video cards on the right."""
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        page.get_style_context().add_class("browse")

        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.get_style_context().add_class("sidebar")
        self.sidebar.set_size_request(SIDEBAR_WIDTH_PX, -1)
        self.sidebar_rows = {}
        for section_id, label, icon_name, _needs_sign_in in SECTIONS:
            row = Gtk.ListBoxRow()
            row.section_id = section_id
            line = Gtk.Box(spacing=16)
            line.pack_start(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU), False, False, 0)
            line.pack_start(make_label(label), False, False, 0)
            row.add(line)
            self.sidebar.add(row)
            self.sidebar_rows[section_id] = row
        self.sidebar.connect("row-activated", lambda _list, row: self.open_section(row.section_id))
        page.pack_start(self.sidebar, False, False, 0)

        self.browse_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.browse_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.browse_scroller.get_vadjustment().connect("value-changed", self.on_browse_scrolled)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.section_title = make_label("Home", "section-title")
        content.pack_start(self.section_title, False, False, 0)
        self.browse_message = make_label("", "secondary", lines=3)
        self.browse_message.set_margin_start(24)
        content.pack_start(self.browse_message, False, False, 0)
        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, valign=Gtk.Align.START)
        self.grid.set_max_children_per_line(6)
        self.grid.set_column_spacing(8)
        self.grid.set_row_spacing(16)
        self.grid.set_margin_start(16)
        self.grid.set_margin_end(16)
        self.grid.connect("child-activated", lambda _grid, card: self.play_item(card.item, self.listed_items))
        content.pack_start(self.grid, False, False, 0)
        self.browse_spinner = Gtk.Spinner(margin=24)
        content.pack_start(self.browse_spinner, False, False, 0)
        self.browse_scroller.add(content)
        page.pack_start(self.browse_scroller, True, True, 0)
        return page

    def _build_watch_page(self):
        """Watch page: video + control bar + title on the left, Up next on the right."""
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        page.get_style_context().add_class("watch")

        main_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_column.pack_start(self._build_video_box(), True, True, 0)

        self.controls_revealer = Gtk.Revealer(reveal_child=True, transition_duration=150)
        self.controls_revealer.add(self._build_control_bar())
        main_column.pack_start(self.controls_revealer, False, False, 0)

        self.watch_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=12)
        self.watch_title = make_label("", "watch-title", lines=2)
        self.watch_channel = make_label("", "secondary")
        self.watch_error_label = make_label("", "error", lines=3)
        # 2026-10-19 smoke_ui.py: a SpongeBob compilation from Home failed with "This video is
        # not available". yt-dlp needs a JavaScript runtime (deno) for some videos and none is
        # installed, so failed videos get a way out: open them in the browser.
        self.open_in_browser_button = Gtk.Button(label=f"Watch in {self.youtube.browser.name}", halign=Gtk.Align.START)
        self.open_in_browser_button.connect("clicked", lambda _button: self.open_current_video_in_browser())
        self.watch_info.pack_start(self.watch_title, False, False, 0)
        self.watch_info.pack_start(self.watch_channel, False, False, 0)
        self.watch_info.pack_start(self.watch_error_label, False, False, 0)
        self.watch_info.pack_start(self.open_in_browser_button, False, False, 0)
        main_column.pack_start(self.watch_info, False, False, 0)
        page.pack_start(main_column, True, True, 0)

        page.pack_start(self._build_side_column(), False, False, 0)
        return page

    def _build_video_box(self):
        """Black box holding the video widget, kept at the video's aspect ratio (black bars around it).

        2026-10-19: nv3dsink always stretches the picture over its whole window, so the
        widget itself has to have the video's shape. The AspectFrame does that, and
        set_video_aspect() updates it for each video (16:9, or 9:16 for Shorts).
        Clicks and pointer movement over the video or the bars are handled on the outer box.
        """
        video_box = Gtk.EventBox(hexpand=True, vexpand=True)
        video_box.get_style_context().add_class("video-background")
        video_box.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
        video_box.connect("button-press-event", self.on_video_clicked)
        video_box.connect("motion-notify-event", lambda *_args: self.show_controls_for_a_while())
        self.video_frame = Gtk.AspectFrame(xalign=0.5, yalign=0.5, ratio=16 / 9, obey_child=False)
        self.video_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self.video_area = Gtk.DrawingArea(can_focus=True)
        self.video_area.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.video_frame.add(self.video_area)
        video_box.add(self.video_frame)
        return video_box

    def set_video_aspect(self, aspect):
        """Reshape the video widget to `aspect` (width / height, float)."""
        self.video_frame.set(0.5, 0.5, aspect, False)

    def _build_control_bar(self):
        """Control bar: play/pause, next, time, seek bar, mute, volume, quality, fullscreen."""
        bar = Gtk.Box(spacing=4)
        bar.get_style_context().add_class("controls")

        self.play_button = Gtk.Button.new_from_icon_name("media-playback-pause-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        self.play_button.set_tooltip_text("Pause / play (Space)")
        self.play_button.connect("clicked", lambda _button: self.player.toggle_pause())
        bar.pack_start(self.play_button, False, False, 0)

        next_button = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        next_button.set_tooltip_text("Next video (Shift+N)")
        next_button.connect("clicked", lambda _button: self.play_next())
        bar.pack_start(next_button, False, False, 0)

        self.time_label = make_label("0:00", "secondary")
        bar.pack_start(self.time_label, False, False, 6)

        self.seek_bar = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek_bar.set_draw_value(False)
        self.seek_bar.set_hexpand(True)
        self.seek_bar.get_style_context().add_class("seek-bar")
        self.seek_bar.connect("change-value", self.on_seek_bar_moved)
        self.seek_bar.connect("button-press-event", lambda *_args: self._set_dragging(True))
        self.seek_bar.connect("button-release-event", lambda *_args: self._set_dragging(False))
        bar.pack_start(self.seek_bar, True, True, 0)

        self.mute_button = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        self.mute_button.set_tooltip_text("Mute (M)")
        self.mute_button.connect("clicked", lambda _button: self.toggle_mute())
        bar.pack_start(self.mute_button, False, False, 0)
        self.volume_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self.volume_slider.set_draw_value(False)
        self.volume_slider.set_size_request(90, -1)
        self.volume_slider.set_value(1.0)
        self.volume_slider.connect("value-changed", lambda slider: self.player.set_volume(slider.get_value()))
        bar.pack_start(self.volume_slider, False, False, 0)

        self.quality_selector = Gtk.ComboBoxText(tooltip_text="Maximum video quality")
        for height in QUALITY_CHOICES_PX:
            self.quality_selector.append(height, f"{height}p")
        self.quality_selector.set_active_id(DEFAULT_QUALITY_PX)
        self.quality_selector.connect("changed", lambda _combo: self.reload_at_new_quality())
        bar.pack_start(self.quality_selector, False, False, 4)

        self.fullscreen_button = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        self.fullscreen_button.set_tooltip_text("Fullscreen (F)")
        self.fullscreen_button.connect("clicked", lambda _button: self.toggle_fullscreen())
        bar.pack_start(self.fullscreen_button, False, False, 0)
        return bar

    def _build_side_column(self):
        """Right-hand column of the watch page: "Up next" and "Comments" tabs.

        self.up_next_column is the whole column (hidden in fullscreen).
        """
        self.up_next_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.up_next_column.set_size_request(UP_NEXT_WIDTH_PX, -1)
        self.side_tabs = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.side_tabs.add_titled(self._build_up_next_tab(), "up_next", "Up next")
        self.side_tabs.add_titled(self._build_comments_tab(), "comments", "Comments")
        self.side_tabs.connect("notify::visible-child-name", lambda *_args: self._on_side_tab_changed())
        switcher = Gtk.StackSwitcher(stack=self.side_tabs, halign=Gtk.Align.CENTER)
        self.up_next_column.pack_start(switcher, False, False, 0)
        self.up_next_column.pack_start(self.side_tabs, True, True, 0)
        return self.up_next_column

    def _build_up_next_tab(self):
        """Up next tab: the autoplay switch, then the list of the next videos."""
        tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header = Gtk.Box(spacing=8)
        header.pack_start(make_label("Autoplay", "secondary", xalign=1.0), True, True, 0)
        self.autoplay_switch = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        header.pack_start(self.autoplay_switch, False, False, 0)
        tab.pack_start(header, False, False, 0)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.up_next_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.up_next_list.get_style_context().add_class("up-next")
        self.up_next_list.connect("row-activated", lambda _list, row: self.play_item(row.item, self.up_next_items))
        scroller.add(self.up_next_list)
        tab.pack_start(scroller, True, True, 0)
        return tab

    def _build_comments_tab(self):
        """Comments tab: "Add a comment" (opens the browser), a status line, then the top comments."""
        tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # 2026-10-19: posting from the app is left out on purpose (see youtube.py, "Comments").
        add_button = Gtk.Button(label=f"Add a comment in {self.youtube.browser.name}")
        add_button.set_tooltip_text("Opens this video on YouTube, where you can comment while signed in")
        add_button.connect("clicked", lambda _button: self.open_current_video_in_browser())
        tab.pack_start(add_button, False, False, 0)
        self.comments_status = make_label("", "secondary", lines=3)
        tab.pack_start(self.comments_status, False, False, 0)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.comments_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroller.add(self.comments_list)
        tab.pack_start(scroller, True, True, 0)
        self.comments_video_id = None
        return tab

    def _on_side_tab_changed(self):
        """Load the comments the first time the Comments tab is opened for the current video."""
        if self.side_tabs.get_visible_child_name() == "comments":
            self._load_comments()

    def _show_comments_placeholder(self):
        """Empty the Comments tab (a new video started); its comments load when the tab is opened."""
        self.comments_video_id = None
        for row in self.comments_list.get_children():
            self.comments_list.remove(row)
        self.comments_status.set_text("")

    def _load_comments(self):
        """Fetch the current video's top comments in a worker thread (once per video)."""
        if self.current_item is None or self.comments_video_id == self.current_item.video_id:
            return
        self.comments_video_id = self.current_item.video_id
        self.comments_status.set_text("Loading comments…")
        threading.Thread(target=self._load_comments_in_background,
                         args=(self.watch_generation, self.current_item.video_id), daemon=True).start()

    def _load_comments_in_background(self, generation, video_id):
        """Worker thread: read the comments (2-3 s), then show them on the main thread."""
        try:
            comments, error_text = self.youtube.get_comments(video_id), None
        except Exception as error:  # comments turned off, network, YouTube changes
            comments, error_text = [], str(error).replace("ERROR: ", "")
        GLib.idle_add(self._show_comments, generation, comments, error_text)

    def _show_comments(self, generation, comments, error_text):
        """Main thread: fill the Comments tab, unless another video started meanwhile."""
        if generation != self.watch_generation:
            return False
        if error_text:
            self.comments_status.set_text(f"Could not load comments: {error_text}")
        elif not comments:
            self.comments_status.set_text("No comments.")
        else:
            self.comments_status.set_text(f"Top {len(comments)} comments")
        for comment in comments:
            self.comments_list.add(CommentRow(comment))
        self.comments_list.show_all()
        return False

    # Sign-in

    def _check_sign_in_in_background(self):
        """Worker thread: read the browser's sign-in (about 3 s), then update the UI and open Home."""
        signed_in = self.youtube.is_signed_in()
        GLib.idle_add(self._apply_sign_in_state, signed_in)

    def _apply_sign_in_state(self, signed_in):
        """Main thread: grey out the personal sections when signed out, then open Home (or search help)."""
        self.signed_in = signed_in
        for section_id, _label, _icon, needs_sign_in in SECTIONS:
            self.sidebar_rows[section_id].set_sensitive(signed_in or not needs_sign_in)
        if signed_in:
            self.account_status_label.set_text(f"Signed in to YouTube through {self.youtube.browser.name}.")
            if self.pages.get_visible_child_name() == "browse" and self.search_query is None:
                self.open_section("home")
        else:
            self.account_status_label.set_text(
                f"Not signed in. Sign in to YouTube in {self.youtube.browser.name}, then choose Refresh sign-in.")
            self.section_title.set_text("Home")
            self.set_browse_message("Search above, or sign in (account button, top right) to see your recommendations.")
        return False

    def open_chromium_sign_in(self):
        """Open YouTube's sign-in page in the browser. The viewer reads that session afterwards."""
        try:
            subprocess.Popen([self.youtube.browser.command, YOUTUBE_SIGN_IN_URL], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            self.account_status_label.set_text(f"Could not open {self.youtube.browser.name}: {error}")
            return
        self.account_status_label.set_text(f"Sign in in {self.youtube.browser.name}, then come back and choose Refresh sign-in.")

    def refresh_sign_in(self):
        """Forget the cookies read at startup and read the browser's session again (after signing in or out)."""
        self.youtube.forget_sign_in()
        self.account_status_label.set_text("Checking sign-in…")
        threading.Thread(target=self._check_sign_in_in_background, daemon=True).start()

    # Browsing

    def set_browse_message(self, text, is_error=False):
        """Show `text` above the grid (in red when is_error). An empty text hides the message."""
        context = self.browse_message.get_style_context()
        (context.add_class if is_error else context.remove_class)("error")
        self.browse_message.set_text(text)
        self.browse_message.set_visible(bool(text))

    def open_section(self, section_id):
        """Show a sidebar section ("home", "subscriptions", ...) from its first page."""
        if not self.signed_in:
            return
        self.show_browse_page()
        self.section_id = section_id
        self.search_query = None
        self.sidebar.select_row(self.sidebar_rows[section_id])
        label = next(label for sid, label, _icon, _sign_in in SECTIONS if sid == section_id)
        self._start_listing(label)

    def on_search_activated(self, entry):
        """Enter in the search box: play a pasted link right away, otherwise search."""
        text = entry.get_text().strip()
        if not text:
            return
        if extract_video_id(text):
            self.play_pasted_url(text)
            return
        self.show_browse_page()
        self.search_query = text
        self.sidebar.unselect_all()
        self._start_listing(f"Results for “{text}”")

    def _start_listing(self, title):
        """Clear the grid, set the section title and load the first page in the background."""
        self.browse_generation += 1
        self.listing_loaded_at = time.monotonic()
        self.listed_items = []
        self.pages_loaded = 0
        self.has_more = True
        for card in self.grid.get_children():
            self.grid.remove(card)
        self.section_title.set_text(title)
        self.set_browse_message("")
        self.browse_scroller.get_vadjustment().set_value(0)
        # 2026-10-19 review: a load still running for the previous section left this True, so
        # the new section never loaded and every later section and search stayed empty.
        self.is_loading_more = False
        self._load_next_page()

    def _load_next_page(self):
        """Fetch the next FEED_PAGE_SIZE videos of the current section or search in a worker thread."""
        if self.is_loading_more or not self.has_more:
            return
        self.is_loading_more = True
        self.browse_spinner.start()
        generation, start_index = self.browse_generation, len(self.listed_items)
        query, section_id, page_index = self.search_query, self.section_id, self.pages_loaded
        threading.Thread(target=self._fetch_page_in_background,
                         args=(generation, start_index, page_index, query, section_id), daemon=True).start()

    def _fetch_page_in_background(self, generation, start_index, page_index, query, section_id):
        """Worker thread: run the search or feed request, then pass the result to the main thread."""
        try:
            if query is not None:
                items, has_more = self.youtube.search(query, start_index=start_index)
            elif section_id == "shorts":
                items, has_more = self.youtube.load_shorts(page_index)
            else:
                items, has_more = self.youtube.load_feed(section_id, start_index=start_index)
            GLib.idle_add(self._add_page, generation, items, has_more, None)
        except SignInError as error:
            GLib.idle_add(self._add_page, generation, [], False, f"Not signed in: {error}")
        except Exception as error:  # yt-dlp raises many error types; every one is shown to the user
            GLib.idle_add(self._add_page, generation, [], False, f"Could not load videos: {error}")

    def _add_page(self, generation, items, has_more, error_text):
        """Main thread: append a page of cards (or show the error). Ignored if a newer listing started."""
        if generation != self.browse_generation:
            return False
        self.is_loading_more = False
        self.browse_spinner.stop()
        if error_text:
            self.set_browse_message(error_text, is_error=True)
            self.has_more = False
            return False
        known_ids = {item.video_id for item in self.listed_items}
        new_items = [item for item in items if item.video_id not in known_ids]
        self.has_more = has_more
        self.pages_loaded += 1
        for item in new_items:
            self.grid.add(VideoCard(item, self.thumbnails))
        self.listed_items.extend(new_items)
        self.grid.show_all()
        if not self.listed_items:
            self.set_browse_message("No videos here.")
        # 2026-10-19 review: when a page does not fill the window there is nothing to scroll,
        # so "value-changed" never fires and the next page never loaded. Check once laid out.
        GLib.idle_add(self._load_more_if_window_not_full, generation)
        return False

    def _load_more_if_window_not_full(self, generation):
        """Idle: load the next page if the grid is still shorter than the window."""
        if generation == self.browse_generation:
            self.on_browse_scrolled(self.browse_scroller.get_vadjustment())
        return False

    def on_browse_scrolled(self, adjustment):
        """Load the next page when the grid is scrolled within LOAD_MORE_MARGIN_PX of the bottom."""
        distance_to_bottom = adjustment.get_upper() - (adjustment.get_value() + adjustment.get_page_size())
        if distance_to_bottom < LOAD_MORE_MARGIN_PX:
            self._load_next_page()

    def show_browse_page(self, refresh_if_stale=False):
        """Stop the video, leave fullscreen and go back to the grid.

        refresh_if_stale: reload the list when it is older than STALE_LISTING_S (used by Back/Escape).
        """
        self.watch_generation += 1
        self.player.stop()
        if self.is_fullscreen:
            self.unfullscreen()
        self.pages.set_visible_child_name("browse")
        self.back_button.hide()
        if refresh_if_stale and time.monotonic() - self.listing_loaded_at > STALE_LISTING_S:
            self.refresh_listing()

    def refresh_listing(self):
        """Reload the current section or search from the start (Refresh button, F5, Ctrl+R)."""
        if self.search_query is not None:
            self.show_browse_page()
            self._start_listing(self.section_title.get_text())
        elif self.signed_in:
            self.open_section(self.section_id)

    # Watching

    def play_pasted_url(self, url):
        """Play a pasted YouTube link (str), from its t= start time if it has one."""
        video_id = extract_video_id(url)
        if video_id is None:
            return
        self.play_item(VideoItem(video_id=video_id, title="Loading…", channel=""), [],
                       resume_at_s=extract_start_time_s(url))

    def play_item(self, item, source_items, resume_at_s=None):
        """Open the watch page and start `item` (VideoItem).

        source_items: the list it was picked from. Up next shows it until YouTube's related
            videos arrive (about 1 s), and keeps showing it if they cannot be loaded.
        resume_at_s: start this many seconds in (quality change, or a link's t=), or None.
        """
        # 2026-10-19 review: the previous video kept playing under the new title while the new
        # one resolved (up to 16 s), and its end could autoplay a different video. A pending
        # seek-bar seek would also have landed on the new video.
        self.player.stop()
        if self.pending_seek_source_id is not None:
            GLib.source_remove(self.pending_seek_source_id)
            self.pending_seek_source_id = None
        if self.current_item is not None and self.current_item.video_id != item.video_id:
            self.watch_history.append(self.current_item)
        self.current_item = item
        self.up_next_items = [other for other in source_items if other.video_id != item.video_id]
        self._fill_up_next()
        self.watch_generation += 1
        # A Short's Up next stays the Shorts list it came from (YouTube's related videos for a
        # Short are mostly regular videos). Comments for the new video load only when the
        # Comments tab is open.
        if not item.is_short:
            threading.Thread(target=self._load_related_in_background,
                             args=(self.watch_generation, item.video_id), daemon=True).start()
        self._show_comments_placeholder()
        if self.side_tabs.get_visible_child_name() == "comments":
            self._load_comments()
        self.resume_position_s = resume_at_s
        self.pages.set_visible_child_name("watch")
        self.back_button.show()
        # 2026-10-19 review: after pasting a link, focus stayed in the search box, so Space, F
        # and the arrows typed into it instead of controlling the video.
        self.video_area.grab_focus()
        self.watch_title.set_text(item.title)
        self.watch_channel.set_text(secondary_line(item))
        self.watch_error_label.hide()
        self.open_in_browser_button.hide()
        self.time_label.set_text("Loading…")
        max_height_px = int(self.quality_selector.get_active_id() or DEFAULT_QUALITY_PX)
        threading.Thread(target=self._resolve_streams_in_background,
                         args=(self.watch_generation, item.video_id, max_height_px), daemon=True).start()
        GLib.timeout_add_seconds(SLOW_LOAD_NOTICE_S, self._explain_slow_load, self.watch_generation)

    def _load_related_in_background(self, generation, video_id):
        """Worker thread: fetch YouTube's related videos, then show them in Up next on the main thread."""
        related = self.youtube.get_related(video_id)
        GLib.idle_add(self._show_related, generation, related)

    def _show_related(self, generation, related):
        """Main thread: replace Up next with the related videos (kept as is when there are none or the user moved on)."""
        if generation == self.watch_generation and related:
            self.up_next_items = related
            self._fill_up_next()
        return False

    def _explain_slow_load(self, generation):
        """Timer: if the video is still resolving after SLOW_LOAD_NOTICE_S, say why it takes longer.

        2026-10-19: videos that YouTube refuses signed out take 12-16 s (about 4 s signed-out
        try, then about 9 s signed in), against about 2 s for most videos.
        """
        if generation == self.watch_generation and not self.player.has_video() \
                and not self.watch_error_label.get_visible():
            self.time_label.set_text("Loading with your sign-in… (about 10 s)")
        return False

    def _resolve_streams_in_background(self, generation, video_id, max_height_px):
        """Worker thread: ask YouTube for the stream URLs (about 1.5 s), then start them on the main thread."""
        try:
            streams = self.youtube.get_streams(video_id, max_height_px,
                                               still_wanted=lambda: generation == self.watch_generation)
            GLib.idle_add(self._start_streams, generation, streams, None)
        except Exception as error:  # LookupError or any yt-dlp error: shown under the video
            GLib.idle_add(self._start_streams, generation, None, str(error))

    def _start_streams(self, generation, streams, error_text):
        """Main thread: load the streams into the player, unless the user moved on meanwhile."""
        if generation != self.watch_generation:
            return False
        if error_text:
            self.show_watch_error(error_text)
            return False
        self.watch_title.set_text(streams.title)
        self.watch_channel.set_text(f"{streams.channel}  ·  {streams.height_px}p{int(streams.fps)}")
        try:
            self.set_video_aspect(streams.aspect)
            self.player.load(streams.video_url, streams.audio_url)
        except RuntimeError as error:
            self.show_watch_error(str(error))
            return False
        self.seek_bar.set_visible(not streams.is_live)
        if self.resume_position_s:
            GLib.timeout_add(1000, self._seek_after_start, generation, self.resume_position_s)
        if self.is_fullscreen:
            self.show_controls_for_a_while()
        return False

    def _seek_after_start(self, generation, position_s):
        """Timer, 1 s after a reload: jump back to where the viewer was before changing quality."""
        if generation == self.watch_generation:
            self.player.seek_to(position_s)
        return False

    def show_watch_error(self, text):
        """Show `text` in red under the video title, with a button to watch it in the browser instead."""
        self.time_label.set_text("")
        # 2026-10-19 (the user's screenshot, "nothing is playing"): the pause icon stayed after
        # a failed video, as if it were playing.
        self.on_player_state(False)
        self.watch_error_label.set_text(text.replace("ERROR: ", ""))
        self.watch_error_label.show()
        self.open_in_browser_button.show()

    def open_current_video_in_browser(self):
        """Open the current video's YouTube page in the browser (fallback when it cannot play here)."""
        if self.current_item is None:
            return
        try:
            subprocess.Popen([self.youtube.browser.command, self.current_item.url], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            self.watch_error_label.set_text(f"Could not open {self.youtube.browser.name}: {error}")

    def _fill_up_next(self):
        """Rebuild the Up next column from self.up_next_items."""
        for row in self.up_next_list.get_children():
            self.up_next_list.remove(row)
        for item in self.up_next_items:
            self.up_next_list.add(UpNextRow(item, self.thumbnails))
        self.up_next_list.show_all()

    def play_previous(self):
        """Go back to the video watched before this one (Shift+P). Does nothing at the start."""
        if self.watch_history:
            previous = self.watch_history.pop()
            self.current_item = None  # so play_item does not push the current video back on
            self.play_item(previous, self.up_next_items)

    def play_next(self):
        """Play the first Up next video. Does nothing when Up next is empty."""
        if self.up_next_items:
            self.play_item(self.up_next_items[0], self.up_next_items)

    def reload_at_new_quality(self):
        """Quality menu changed: restart the current video at the new height, at the same position."""
        if self.current_item is None or self.pages.get_visible_child_name() != "watch":
            return
        position_s = self.player.position_s()
        self.play_item(self.current_item, [self.current_item] + self.up_next_items, resume_at_s=position_s)

    # Player callbacks

    def on_player_progress(self, position_s, duration_s):
        """Player: update the time label and seek bar (unless the user is dragging it)."""
        if position_s is None or time.monotonic() < self.notice_until:
            return
        if duration_s is None:
            self.time_label.set_text(f"● LIVE  {format_duration(position_s)}")
            return
        self.time_label.set_text(f"{format_duration(position_s)} / {format_duration(duration_s)}")
        if not self.user_is_dragging_seek_bar and self.pending_seek_source_id is None:
            self.seek_bar.set_range(0, duration_s)
            self.seek_bar.set_value(position_s)

    def on_player_state(self, is_playing):
        """Player: switch the play button icon between pause (while playing) and play (while paused)."""
        icon_name = "media-playback-pause-symbolic" if is_playing else "media-playback-start-symbolic"
        self.play_button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR))

    def on_player_finished(self, error_text):
        """Player: the video ended or failed. Autoplay the next one, or show the error."""
        if error_text:
            self.show_watch_error(error_text)
            return
        if self.autoplay_switch.get_active() and self.up_next_items:
            self.play_next()
        else:
            self.time_label.set_text("Finished")

    # Seek bar

    def _set_dragging(self, is_dragging):
        """Remember whether the mouse button is down on the seek bar, so progress updates do not fight the drag."""
        self.user_is_dragging_seek_bar = is_dragging
        return False

    def on_seek_bar_moved(self, _scale, _scroll_type, value):
        """User moved the seek bar: seek there once it has been still for SEEK_BAR_DEBOUNCE_MS."""
        if self.pending_seek_source_id is not None:
            GLib.source_remove(self.pending_seek_source_id)
        self.pending_seek_source_id = GLib.timeout_add(SEEK_BAR_DEBOUNCE_MS, self._apply_pending_seek, value)
        return False

    def _apply_pending_seek(self, position_s):
        """Timer: perform the seek the seek bar asked for."""
        self.pending_seek_source_id = None
        self.player.seek_to(position_s)
        return False

    # Fullscreen and controls

    def toggle_fullscreen(self):
        """Switch fullscreen on or off (watch page only)."""
        if self.pages.get_visible_child_name() != "watch":
            return
        if self.is_fullscreen:
            self.unfullscreen()
        else:
            self.fullscreen()

    def on_window_state_changed(self, _window, event):
        """Window manager confirmed (un)fullscreen: hide or show everything around the video."""
        is_fullscreen = bool(event.new_window_state & Gdk.WindowState.FULLSCREEN)
        if is_fullscreen == self.is_fullscreen:
            return False
        self.is_fullscreen = is_fullscreen
        for widget in (self.header, self.watch_info, self.up_next_column):
            widget.set_visible(not is_fullscreen)
        icon_name = "view-restore-symbolic" if is_fullscreen else "view-fullscreen-symbolic"
        self.fullscreen_button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR))
        if is_fullscreen:
            self.show_controls_for_a_while()
        else:
            self._cancel_controls_hide()
            self.controls_revealer.set_reveal_child(True)
            self._set_pointer_visible(True)
        return False

    def show_controls_for_a_while(self):
        """Show the control bar and pointer. In fullscreen, hide them again after CONTROLS_HIDE_DELAY_S."""
        self.controls_revealer.set_reveal_child(True)
        self._set_pointer_visible(True)
        if not self.is_fullscreen:
            return
        self._cancel_controls_hide()
        self.controls_hide_source_id = GLib.timeout_add_seconds(CONTROLS_HIDE_DELAY_S, self._hide_controls)

    def _cancel_controls_hide(self):
        """Cancel a pending auto-hide of the control bar."""
        if self.controls_hide_source_id is not None:
            GLib.source_remove(self.controls_hide_source_id)
            self.controls_hide_source_id = None

    def _hide_controls(self):
        """Timer: hide the control bar and pointer (fullscreen only)."""
        self.controls_hide_source_id = None
        if self.is_fullscreen:
            self.controls_revealer.set_reveal_child(False)
            self._set_pointer_visible(False)
        return False

    def _set_pointer_visible(self, visible):
        """Show or hide the mouse pointer over the video."""
        gdk_window = self.video_area.get_window()
        if gdk_window is None:
            return
        cursor = None if visible else Gdk.Cursor.new_from_name(gdk_window.get_display(), "none")
        gdk_window.set_cursor(cursor)

    # Mouse and keyboard

    def on_video_clicked(self, _widget, event):
        """Click on the video pauses or plays; a double-click toggles fullscreen."""
        if event.button != Gdk.BUTTON_PRIMARY:
            return False
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            # The two single clicks before this already paused and resumed, so the play state is unchanged.
            self.toggle_fullscreen()
        elif event.type == Gdk.EventType.BUTTON_PRESS:
            self.player.toggle_pause()
        self.show_controls_for_a_while()
        return True

    def toggle_mute(self):
        """Mute or unmute and update the speaker icon."""
        muted = self.player.toggle_mute()
        icon_name = "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        self.mute_button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR))

    def on_key_press(self, _window, event):
        """Keyboard shortcuts (listed in the module docstring). Typing in the search box is left alone."""
        if self.search_entry.has_focus():
            if event.keyval == Gdk.KEY_Escape:
                self.video_area.grab_focus()
                return True
            return False
        if event.keyval == Gdk.KEY_slash:
            self.search_entry.grab_focus()
            return True
        control_pressed = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if event.keyval == Gdk.KEY_F5 or (control_pressed and event.keyval == Gdk.KEY_r):
            self.refresh_listing()
            return True
        if self.pages.get_visible_child_name() != "watch":
            return False
        handled = self._handle_watch_key(event.keyval)
        if handled:
            self.show_controls_for_a_while()
        return handled

    def _handle_watch_key(self, keyval):
        """Run the watch-page action for one key. Returns True when the key was used."""
        actions = {
            Gdk.KEY_space: self.player.toggle_pause,
            Gdk.KEY_k: self.player.toggle_pause,
            Gdk.KEY_f: self.toggle_fullscreen,
            Gdk.KEY_Left: lambda: self.player.seek_by(-SEEK_SMALL_S),
            Gdk.KEY_Right: lambda: self.player.seek_by(SEEK_SMALL_S),
            Gdk.KEY_j: lambda: self.player.seek_by(-SEEK_LARGE_S),
            Gdk.KEY_l: lambda: self.player.seek_by(SEEK_LARGE_S),
            Gdk.KEY_Up: lambda: self.volume_slider.set_value(self.volume_slider.get_value() + VOLUME_STEP),
            Gdk.KEY_Down: lambda: self.volume_slider.set_value(self.volume_slider.get_value() - VOLUME_STEP),
            Gdk.KEY_m: self.toggle_mute,
            Gdk.KEY_N: self.play_next,
            Gdk.KEY_P: self.play_previous,
            Gdk.KEY_Home: lambda: self.player.seek_to(0),
            Gdk.KEY_bracketleft: lambda: self.change_audio_delay(-AUDIO_DELAY_STEP_MS),
            Gdk.KEY_bracketright: lambda: self.change_audio_delay(AUDIO_DELAY_STEP_MS),
            Gdk.KEY_Escape: self._escape,
        }
        # Keys 0-9 jump to 0%-90% of the video, as on YouTube.
        for digit in range(10):
            actions[getattr(Gdk, f"KEY_{digit}")] = lambda digit=digit: self._seek_to_fraction(digit / 10)
        action = actions.get(keyval)
        if action is None:
            return False
        action()
        return True

    def change_audio_delay(self, step_ms):
        """[ / ]: move the sound earlier or later by step_ms, save it, and show the new value for a moment."""
        delay_ms = max(-AUDIO_DELAY_LIMIT_MS, min(AUDIO_DELAY_LIMIT_MS, self.player.audio_delay_ms + step_ms))
        self.player.set_audio_delay(delay_ms)
        self.settings["audio_delay_ms"] = delay_ms
        save_settings(self.settings)
        self.show_notice(f"Audio delay: {delay_ms:+d} ms" if delay_ms else "Audio delay: off")

    def show_notice(self, text):
        """Show `text` in the time label for NOTICE_DURATION_S seconds."""
        self.time_label.set_text(text)
        self.notice_until = time.monotonic() + NOTICE_DURATION_S

    def _seek_to_fraction(self, fraction):
        """Jump to `fraction` (0.0-1.0) of the video's length. Does nothing for live streams."""
        duration_s = self.player.duration_s()
        if duration_s:
            self.player.seek_to(duration_s * fraction)

    def _escape(self):
        """Escape: leave fullscreen if in it, otherwise go back to the grid."""
        if self.is_fullscreen:
            self.unfullscreen()
        else:
            self.show_browse_page(refresh_if_stale=True)

    def on_destroy(self, _window):
        """Window closed: stop the video, drop pending thumbnail downloads and quit."""
        self.player.shutdown()
        self.thumbnails.shutdown()
        Gtk.main_quit()
