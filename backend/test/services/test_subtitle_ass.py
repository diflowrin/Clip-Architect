import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import VideoParams
from app.services import subtitle_ass as sa
from app.utils import utils


def _params(**overrides):
    values = {
        "video_subject": "test",
        "font_size": 64,
        "text_fore_color": "#FFE44D",
        "stroke_color": "#000000",
        "stroke_width": 2.5,
        "subtitle_position": "bottom",
    }
    values.update(overrides)
    return VideoParams(**values)


def _event(text="hello", start=0.0, end=1.0, text_height=80, block_height=140):
    return sa.SubtitleEvent(
        start=start,
        end=end,
        text=text,
        text_height=text_height,
        block_height=block_height,
        descender=20,
    )


class TestAssColor(unittest.TestCase):
    def test_channels_are_reversed_and_alpha_is_transparency(self):
        """&HAABBGGRR, and 00 alpha means opaque - both easy to get backwards."""
        self.assertEqual(sa.ass_color("#FFE44D"), "&H004DE4FF")
        self.assertEqual(sa.ass_color("#000000"), "&H00000000")
        self.assertEqual(sa.ass_color("#FF0000", alpha=0x80), "&H800000FF")

    def test_short_form_is_expanded(self):
        self.assertEqual(sa.ass_color("#F00"), sa.ass_color("#FF0000"))

    def test_an_unusable_colour_falls_back_rather_than_raising(self):
        """A malformed colour must not be able to fail a render."""
        self.assertEqual(sa.ass_color("not a colour", default="#FFFFFF"), "&H00FFFFFF")


class TestFontSize(unittest.TestCase):
    """
    ASS sizes a font by its whole Windows line box, not by its em square, so
    the form's pixel size is not the number that goes in the file. Getting this
    wrong drew Charm at less than half the size it used to be.
    """

    def test_pixel_size_is_converted_using_the_font_metrics(self):
        charm = os.path.join(utils.font_dir(), "Charm-Bold.ttf")
        if not os.path.exists(charm):
            self.skipTest("Charm-Bold.ttf is not installed")
        # Measured against Pillow: 64 px of Charm needs an ASS size near 151.
        self.assertAlmostEqual(sa.ass_font_size(charm, 64), 151, delta=2)

    def test_the_ratio_is_per_font_not_a_constant(self):
        charm = os.path.join(utils.font_dir(), "Charm-Bold.ttf")
        noto = os.path.join(utils.font_dir(), "NotoSans-Bold.ttf")
        if not (os.path.exists(charm) and os.path.exists(noto)):
            self.skipTest("bundled fonts are not installed")
        self.assertNotEqual(sa.ass_font_size(charm, 64), sa.ass_font_size(noto, 64))

    def test_an_unreadable_font_falls_back_to_the_pixel_size(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = os.path.join(directory, "not-a-font.ttf")
            with open(fake, "wb") as handle:
                handle.write(b"nope")
            self.assertEqual(sa.ass_font_size(fake, 64), 64)


class TestSupports(unittest.TestCase):
    def test_a_rounded_background_is_refused(self):
        """ASS boxes are square; drawing one anyway would change how videos look."""
        ok, reason = sa.supports(
            _params(text_background_color="#000000", rounded_subtitle_background=True)
        )
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_a_square_background_is_fine(self):
        ok, _ = sa.supports(
            _params(text_background_color="#000000", rounded_subtitle_background=False)
        )
        self.assertTrue(ok)

    def test_no_background_is_fine(self):
        self.assertTrue(sa.supports(_params())[0])


class TestBuild(unittest.TestCase):
    def _build(self, params, events=None):
        font = os.path.join(utils.font_dir(), "NotoSans-Bold.ttf")
        if not os.path.exists(font):
            self.skipTest("NotoSans-Bold.ttf is not installed")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "s.ass")
            sa.build(
                events if events is not None else [_event()],
                path,
                params=params,
                video_width=1080,
                video_height=1920,
                font_path=font,
            )
            with open(path, encoding="utf-8-sig") as handle:
                return handle.read()

    def test_declared_resolution_matches_the_frame(self):
        """Sizes and margins are in real pixels only while these agree."""
        content = self._build(_params())
        self.assertIn("PlayResX: 1080", content)
        self.assertIn("PlayResY: 1920", content)

    def test_libass_does_not_rewrap_the_lines(self):
        """The text arrives wrapped; libass breaking it again would disagree."""
        self.assertIn(f"WrapStyle: {sa._WRAP_NONE}", self._build(_params()))

    def test_a_background_keeps_the_outline(self):
        """BorderStyle 3 would replace the stroke with the box; 4 keeps both."""
        content = self._build(_params(text_background_color="#000000"))
        style = next(l for l in content.splitlines() if l.startswith("Style:"))
        self.assertIn(",4,", style)

    def test_newlines_become_ass_line_breaks(self):
        content = self._build(_params(), [_event(text="one" + chr(10) + "two")])
        self.assertIn("one" + chr(92) + "Ntwo", content)

    def test_braces_in_a_subtitle_are_not_read_as_markup(self):
        """An unescaped brace opens an override block and eats the text."""
        content = self._build(_params(), [_event(text="a {b} c")])
        self.assertNotIn(",,a {b} c", content)
        self.assertIn(chr(92) + "{b" + chr(92) + "}", content)

    def test_a_zero_length_subtitle_is_dropped(self):
        content = self._build(_params(), [_event(start=2.0, end=2.0)])
        self.assertNotIn("Dialogue:", content)

    def test_top_and_bottom_use_different_alignments(self):
        top = self._build(_params(subtitle_position="top"))
        bottom = self._build(_params(subtitle_position="bottom"))
        self.assertNotEqual(
            [l for l in top.splitlines() if l.startswith("Dialogue:")],
            [l for l in bottom.splitlines() if l.startswith("Dialogue:")],
        )

    def test_timestamps_are_centiseconds(self):
        content = self._build(_params(), [_event(start=1.5, end=3.25)])
        self.assertIn("0:00:01.50,0:00:03.25", content)


class TestParseSrtTime(unittest.TestCase):
    def test_comma_and_dot_are_both_accepted(self):
        self.assertAlmostEqual(sa.parse_srt_time("00:00:02,475"), 2.475, places=3)
        self.assertAlmostEqual(sa.parse_srt_time("00:00:02.475"), 2.475, places=3)

    def test_hours_and_minutes_count(self):
        self.assertAlmostEqual(sa.parse_srt_time("01:02:03,000"), 3723.0, places=3)

    def test_a_malformed_timestamp_raises(self):
        with self.assertRaises(ValueError):
            sa.parse_srt_time("not a time")


if __name__ == "__main__":
    unittest.main()
