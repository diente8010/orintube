"""Unit tests for the parts of OrinTube that need no network, screen or decoder.

Run (from anywhere):  python3 -m unittest discover -s ~/orintube/tests -v
Uses only the standard library (pytest is not installed on the Jetson).
The format lists below are trimmed copies of what yt-dlp 2026.08.19 returned on
2026-10-19 for Big Buck Bunny (aqz-KE-bpKQ) and a CNN-News18 live stream.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from orintube.ui import format_duration, format_views, secondary_line  # noqa: E402
from orintube.youtube import (  # noqa: E402
    KNOWN_BROWSERS, VideoItem, choose_hls_formats, choose_muxed_hls_format, entry_to_video_item,
    comment_from_entry, extract_start_time_s, extract_video_id, find_browser, parse_clock_duration,
    parse_related_videos, parse_short_count)


def hls(format_id, vcodec, height=None, fps=None, tbr=None, note=None):
    """Build one yt-dlp HLS format dict like the ones extract_info() returns."""
    return {"format_id": format_id, "protocol": "m3u8_native", "vcodec": vcodec, "acodec": None,
            "height": height, "fps": fps, "tbr": tbr, "format_note": note, "url": f"https://hls/{format_id}"}


BIG_BUCK_BUNNY_FORMATS = [
    hls("233", "none", note="Default, low"),
    hls("234", "none", note="Default, high"),
    hls("269", "avc1.4D400C", 144, 30, 169),
    hls("230", "avc1.4D401E", 360, 30, 813),
    hls("311", "avc1.4D4020", 720, 60, 4553),
    hls("612", "vp09.00.40.08", 720, 60, 3042),
    hls("312", "avc1.64002A", 1080, 60, 7417),
    hls("617", "vp09.00.41.08", 1080, 60, 5022),
    hls("628", "vp09.00.51.08", 2160, 60, 27987),
    {"format_id": "299", "protocol": "https", "vcodec": "avc1.64002A", "height": 1080, "url": "https://dash/299"},
]


class ChooseHlsFormatsTest(unittest.TestCase):
    def test_picks_tallest_h264_at_or_below_limit_and_high_audio(self):
        video, audio = choose_hls_formats(BIG_BUCK_BUNNY_FORMATS, 1080)
        self.assertEqual(video["format_id"], "312")
        self.assertEqual(audio["format_id"], "234")

    def test_respects_lower_limit_and_ignores_vp9(self):
        video, _audio = choose_hls_formats(BIG_BUCK_BUNNY_FORMATS, 720)
        self.assertEqual(video["format_id"], "311")

    def test_vertical_short_quality_is_its_shorter_side(self):
        formats = [dict(hls("299", "avc1.640028", 1920, 30, 4000), width=1080),
                   dict(hls("298", "avc1.4d401f", 1280, 30, 2500), width=720), BIG_BUCK_BUNNY_FORMATS[1]]
        video, _audio = choose_hls_formats(formats, 1080)
        self.assertEqual(video["format_id"], "299")
        video, _audio = choose_hls_formats(formats, 720)
        self.assertEqual(video["format_id"], "298")

    def test_ignores_dash_formats(self):
        dash_only = [f for f in BIG_BUCK_BUNNY_FORMATS if f["protocol"] == "https"] + BIG_BUCK_BUNNY_FORMATS[:2]
        with self.assertRaises(LookupError):
            choose_hls_formats(dash_only, 1080)

    def test_no_audio_raises(self):
        with self.assertRaises(LookupError):
            choose_hls_formats([hls("312", "avc1.64002A", 1080, 60, 7417)], 1080)

    def test_falls_back_to_low_audio(self):
        formats = [hls("312", "avc1.64002A", 1080, 60, 7417), hls("233", "none", note="Default, low")]
        _video, audio = choose_hls_formats(formats, 1080)
        self.assertEqual(audio["format_id"], "233")


class ChooseMuxedHlsFormatTest(unittest.TestCase):
    # Signed-in web_safari formats for Big Buck Bunny, 2026-10-19 (video + audio in one playlist).
    FORMATS = [dict(hls("93", "avc1.4D401E", 360, 30, 813), acodec="mp4a.40.2"),
               dict(hls("300", "avc1.4D4020", 720, 60, 4553), acodec="mp4a.40.2"),
               dict(hls("301", "avc1.64002A", 1080, 60, 7417), acodec="mp4a.40.2"),
               hls("312", "avc1.64002A", 1080, 60, 7417)]

    def test_picks_tallest_muxed_within_limit(self):
        self.assertEqual(choose_muxed_hls_format(self.FORMATS, 1080)["format_id"], "301")
        self.assertEqual(choose_muxed_hls_format(self.FORMATS, 720)["format_id"], "300")

    def test_video_only_formats_are_not_muxed(self):
        with self.assertRaises(LookupError):
            choose_muxed_hls_format(self.FORMATS[3:], 1080)


class FindBrowserTest(unittest.TestCase):
    def test_first_existing_profile_wins(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".config/google-chrome"))
            self.assertEqual(find_browser(home).name, "Google Chrome")
            os.makedirs(os.path.join(home, "snap/chromium/common/chromium"))
            self.assertEqual(find_browser(home).profile_dir, "~/snap/chromium/common/chromium")

    def test_no_browser_falls_back_to_first(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(find_browser(home), KNOWN_BROWSERS[0])


def lockup(video_id, title, channel, views, badge, content_type="LOCKUP_CONTENT_TYPE_VIDEO"):
    """A trimmed lockupViewModel shaped like the watch page's on 2026-10-19."""
    return {"lockupViewModel": {
        "contentId": video_id, "contentType": content_type,
        "contentImage": {"thumbnailViewModel": {"overlays": [
            {"thumbnailBottomOverlayViewModel": {"badges": [{"thumbnailBadgeViewModel": {"text": badge}}]}}]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": title},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": channel}}]},
                {"metadataParts": [{"text": {"content": views}}, {"text": {"content": "2y ago"}}]}]}}}}}}


class RelatedVideosTest(unittest.TestCase):
    PAGE = {"contents": {"twoColumnWatchNextResults": {
        "results": {"results": {"contents": [lockup("PRIMARYxxxx", "not related", "x", "1", "1:00")]}},
        "secondaryResults": {"secondaryResults": {"results": [{"itemSectionRenderer": {"contents": [
            lockup("NdSg9-Qkp7c", "BEST of SpongeBob Season 8!", "SpongeBob SquarePants Official", "20M", "2:50:33"),
            lockup("6L2ADLzFgNc", "the video itself", "SpongeBob SquarePants Official", "1M", "2:04:24"),
            lockup("PLaylist123", "a playlist", "someone", "5", "12 videos", "LOCKUP_CONTENT_TYPE_PLAYLIST"),
            lockup("_PtgZga5uss", "SpongeBob Sets Sail", "Nicktoons", "1.1M", "1:10:04"),
            lockup("NdSg9-Qkp7c", "duplicate", "x", "1", "1:00"),
            {"continuationItemRenderer": {}}]}}]}}}}}

    def test_reads_related_column_in_order_without_self_playlists_or_duplicates(self):
        items = parse_related_videos(self.PAGE, exclude_video_id="6L2ADLzFgNc")
        self.assertEqual([item.video_id for item in items], ["NdSg9-Qkp7c", "_PtgZga5uss"])
        self.assertEqual(items[0], VideoItem("NdSg9-Qkp7c", "BEST of SpongeBob Season 8!",
                                             "SpongeBob SquarePants Official", 10233, 20_000_000))

    def test_unknown_layout_gives_empty_list(self):
        self.assertEqual(parse_related_videos({"contents": {}}), [])

    def test_short_counts_and_clock_durations(self):
        self.assertEqual(parse_short_count("1.1M"), 1_100_000)
        self.assertEqual(parse_short_count("956K views"), 956_000)
        self.assertEqual(parse_short_count("835"), 835)
        self.assertIsNone(parse_short_count("No views"))
        self.assertEqual(parse_clock_duration("4:02:01"), 14521)
        self.assertEqual(parse_clock_duration("23:18"), 1398)
        self.assertIsNone(parse_clock_duration("LIVE"))


class ExtractVideoIdTest(unittest.TestCase):
    def test_link_forms(self):
        for link in ("https://www.youtube.com/watch?v=aqz-KE-bpKQ",
                     "youtube.com/watch?feature=share&v=aqz-KE-bpKQ",
                     "https://youtu.be/aqz-KE-bpKQ?t=30",
                     "https://m.youtube.com/shorts/aqz-KE-bpKQ",
                     "https://www.youtube.com/live/aqz-KE-bpKQ",
                     "  https://music.youtube.com/watch?v=aqz-KE-bpKQ&list=RD  "):
            self.assertEqual(extract_video_id(link), "aqz-KE-bpKQ", link)

    def test_search_words_are_not_links(self):
        for text in ("big buck bunny", "youtube tutorial", "https://vimeo.com/123456"):
            self.assertIsNone(extract_video_id(text), text)


class EntryToVideoItemTest(unittest.TestCase):
    def test_flat_feed_entry(self):
        item = entry_to_video_item({"id": "uo12mR6wy7s", "title": "Mavericks vs Hawks", "duration": 2627.0,
                                    "channel": "HiepNBA Highlight", "view_count": 9900})
        self.assertEqual(item, VideoItem("uo12mR6wy7s", "Mavericks vs Hawks", "HiepNBA Highlight", 2627, 9900))
        self.assertEqual(item.url, "https://www.youtube.com/watch?v=uo12mR6wy7s")

    def test_live_entry_has_no_duration_and_uses_uploader(self):
        item = entry_to_video_item({"id": "qsc1NQAfZnQ", "title": "Live", "uploader": "CNN-News18"})
        self.assertIsNone(item.duration_s)
        self.assertEqual(item.channel, "CNN-News18")

    def test_channels_and_playlists_are_skipped(self):
        self.assertIsNone(entry_to_video_item({"id": "UCabcdefghijklmnopqrstuv", "title": "A channel"}))
        self.assertIsNone(entry_to_video_item({"title": "no id"}))


class StartTimeTest(unittest.TestCase):
    def test_link_start_times(self):
        self.assertEqual(extract_start_time_s("https://youtu.be/aqz-KE-bpKQ?t=90"), 90)
        self.assertEqual(extract_start_time_s("https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=1m30s"), 90)
        self.assertEqual(extract_start_time_s("watch?v=aqz-KE-bpKQ&t=1h2m3s&list=x"), 3723)
        self.assertEqual(extract_start_time_s("watch?v=aqz-KE-bpKQ&start=45"), 45)
        self.assertIsNone(extract_start_time_s("https://www.youtube.com/watch?v=aqz-KE-bpKQ"))


class CommentAndShortTest(unittest.TestCase):
    def test_top_level_comment(self):
        comment = comment_from_entry({"parent": "root", "text": " Ah.. The classic test video ", "author": "@Seed",
                                      "like_count": 1900, "_time_text": "2 years ago", "is_pinned": False})
        self.assertEqual((comment.author, comment.text, comment.like_count, comment.time_text),
                         ("@Seed", "Ah.. The classic test video", 1900, "2 years ago"))

    def test_replies_and_empty_comments_are_skipped(self):
        self.assertIsNone(comment_from_entry({"parent": "UgxABC", "text": "a reply"}))
        self.assertIsNone(comment_from_entry({"parent": "root", "text": ""}))

    def test_shorts_tab_entry_is_a_short_with_a_jpeg_cover(self):
        item = entry_to_video_item({"id": "btKXlW-ouWI", "title": "Steph Curry", "url": "https://www.youtube.com/shorts/btKXlW-ouWI"})
        self.assertTrue(item.is_short)
        self.assertEqual(item.thumbnail_url, "https://i.ytimg.com/vi/btKXlW-ouWI/oar2.jpg")
        regular = entry_to_video_item({"id": "aqz-KE-bpKQ", "title": "x", "url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ"})
        self.assertFalse(regular.is_short)


class FormattingTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(format_duration(5), "0:05")
        self.assertEqual(format_duration(635), "10:35")
        self.assertEqual(format_duration(7464), "2:04:24")
        self.assertEqual(format_duration(None), "LIVE")

    def test_views(self):
        self.assertEqual(format_views(835), "835 views")
        self.assertEqual(format_views(9900), "9.9K views")
        self.assertEqual(format_views(2_200_000), "2.2M views")
        self.assertEqual(format_views(1_000_000), "1M views")
        self.assertEqual(format_views(None), "")

    def test_secondary_line_skips_missing_parts(self):
        self.assertEqual(secondary_line(VideoItem("x" * 11, "t", "Blender", 1, 2_200_000)), "Blender  ·  2.2M views")
        self.assertEqual(secondary_line(VideoItem("x" * 11, "t", "", 1, None)), "")


if __name__ == "__main__":
    unittest.main()
