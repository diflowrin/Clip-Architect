import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import VideoTransitionMode
from app.services import video_transitions as vt

# Everything an FFmpeg new enough for the app's own bundled build offers.
_MODERN = frozenset(
    {
        "fade",
        "fadeblack",
        "slideleft",
        "slideright",
        "coverleft",
        "revealleft",
        "zoomin",
        "circlecrop",
        "circleclose",
    }
)
# What xfade had when the filter first shipped. `get_ffmpeg_binary` prefers an
# ffmpeg already on PATH, which can be years older than the one bundled.
_OLD = frozenset({"fade", "fadeblack", "slideleft", "slideright", "circleclose"})


class TestSupportedTransitions(unittest.TestCase):
    """
    Read from the filter's own help output. A transition name this build does
    not have fails the render at the last step, after every clip has already
    been encoded.
    """

    def _probe(self, stdout, returncode=0):
        vt.supported_transitions.cache_clear()
        result = types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
        with patch.object(vt.subprocess, "run", return_value=result):
            return vt.supported_transitions("ffmpeg")

    def test_transition_names_are_read_from_the_option_list(self):
        names = self._probe(
            "xfade AVOptions:\n"
            "   transition        <int>        ..FV....... set transition\n"
            "     custom          -1           ..FV....... custom transition\n"
            "     fade            0            ..FV....... fade transition\n"
            "     wipeleft        1            ..FV....... wipe left transition\n"
            "   duration          <duration>   ..FV....... set duration\n"
            "   expr              <string>     ..FV....... set expression\n"
        )
        self.assertEqual(names, frozenset({"custom", "fade", "wipeleft"}))
        # The filter's own options are not transitions and must not be offered
        # as one: `duration` would be accepted by the parser and rejected by
        # FFmpeg.
        self.assertNotIn("duration", names)
        self.assertNotIn("expr", names)

    def test_an_ffmpeg_without_xfade_reports_nothing(self):
        """
        `-h filter=xfade` answers "Unknown filter" on stdout and still exits
        zero, so the return code alone does not settle it.
        """
        self.assertEqual(self._probe("Unknown filter 'xfade'.\n"), frozenset())

    def test_a_failed_probe_reports_nothing_rather_than_raising(self):
        vt.supported_transitions.cache_clear()
        with patch.object(vt.subprocess, "run", side_effect=OSError("no ffmpeg")):
            self.assertEqual(vt.supported_transitions("ffmpeg"), frozenset())


class TestResolveTransition(unittest.TestCase):
    def _resolve(self, mode, available=_MODERN):
        with patch.object(vt, "supported_transitions", return_value=available):
            return vt.resolve_transition(mode, "ffmpeg")

    def test_no_transition_means_hard_cuts(self):
        for mode in (None, "", VideoTransitionMode.none):
            with self.subTest(mode=mode):
                self.assertIsNone(self._resolve(mode))

    def test_every_mode_resolves_to_a_real_transition(self):
        expected = {
            VideoTransitionMode.fade_in: "fade",
            # The old FadeOut dipped to black at the end of every clip; this
            # keeps that look and makes it one dip at the cut rather than two.
            VideoTransitionMode.fade_out: "fadeblack",
            # In slides over the outgoing shot, out slides away to uncover the
            # incoming one - which is what the two settings described when
            # they were per-clip effects.
            VideoTransitionMode.slide_in: "coverleft",
            VideoTransitionMode.slide_out: "revealleft",
            VideoTransitionMode.zoom_in: "zoomin",
            # There is no `zoomout`. `circleclose` is the closest thing that
            # still reads as the outgoing shot receding, and unlike `rectcrop`
            # it does not pass through a black frame on the way.
            VideoTransitionMode.zoom_out: "circleclose",
        }
        for mode, transition in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(self._resolve(mode), transition)

    def test_the_value_is_accepted_as_well_as_the_enum(self):
        self.assertEqual(self._resolve("SlideOut"), self._resolve(VideoTransitionMode.slide_out))

    def test_an_older_ffmpeg_gets_the_nearest_transition_it_has(self):
        """A missing name must degrade, not fail a render at the last step."""
        self.assertEqual(self._resolve(VideoTransitionMode.slide_in, _OLD), "slideleft")
        self.assertEqual(self._resolve(VideoTransitionMode.slide_out, _OLD), "slideright")
        self.assertEqual(self._resolve(VideoTransitionMode.zoom_in, _OLD), "fade")

    def test_an_ffmpeg_without_xfade_falls_back_to_hard_cuts(self):
        self.assertIsNone(self._resolve(VideoTransitionMode.fade_in, frozenset()))

    def test_shuffle_picks_one_transition_for_the_whole_render(self):
        """
        It used to draw again at every cut, so three cuts in the viewer had
        seen a dissolve, a slide and a zoom: that reads as a mistake, not a
        style. resolve_transition is called once per render, so one draw here
        is one look for the video.
        """
        with patch.object(vt.random, "choice", return_value="ZoomIn") as choice:
            resolved = self._resolve(VideoTransitionMode.shuffle)
        self.assertEqual(resolved, "zoomin")
        self.assertEqual(choice.call_count, 1)

    def test_shuffle_never_draws_shuffle_or_none(self):
        drawn = set()
        for _ in range(200):
            drawn.add(self._resolve(VideoTransitionMode.shuffle))
        self.assertNotIn(None, drawn)
        self.assertEqual(
            drawn,
            {"fade", "fadeblack", "coverleft", "revealleft", "zoomin", "circleclose"},
        )


class TestOverlaps(unittest.TestCase):
    """
    How much of the timeline each cut eats. The whole point of a transition is
    that the two clips are on screen at once, so the video is shorter than the
    clips that went into it.
    """

    def test_a_join_takes_the_full_transition_when_both_clips_can_spare_it(self):
        self.assertEqual(vt.overlaps_for([5.0, 5.0, 5.0], 1.0), [1.0, 1.0])

    def test_nothing_is_taken_without_a_transition(self):
        self.assertEqual(vt.overlaps_for([5.0, 5.0], 0.0), [])

    def test_a_single_clip_has_no_joins(self):
        self.assertEqual(vt.overlaps_for([5.0], 1.0), [])
        self.assertEqual(vt.overlaps_for([], 1.0), [])

    def test_a_short_clip_is_never_more_than_half_transition(self):
        """
        The scene planner leaves the short tail of a source file as its own
        clip. A one-second dissolve across a 1.2-second tail would leave
        nothing of the tail to see.
        """
        self.assertEqual(vt.overlaps_for([5.0, 1.2], 1.0), [0.6])

    def test_two_short_clips_in_a_row_cannot_be_consumed_twice(self):
        """
        The second join is measured against what the first one left of the
        middle clip, not against its full length.
        """
        overlaps = vt.overlaps_for([5.0, 1.2, 5.0], 1.0)
        self.assertEqual(overlaps[0], 0.6)
        self.assertAlmostEqual(overlaps[1], 0.3)
        self.assertLess(sum(overlaps), 1.2)

    def test_the_timeline_is_the_clips_minus_the_overlaps(self):
        self.assertAlmostEqual(vt.timeline_length([5.0, 5.0, 5.0, 5.0], 1.0), 17.0)
        self.assertAlmostEqual(vt.timeline_length([5.0, 5.0, 5.0, 5.0], 0.0), 20.0)

    def test_a_transition_always_makes_the_video_shorter_than_its_clips(self):
        for durations in ([5.0, 5.0], [3.0, 0.4, 12.0], [1.0] * 6):
            with self.subTest(durations=durations):
                self.assertLess(
                    vt.timeline_length(durations, 1.0), sum(durations)
                )
                self.assertGreater(vt.timeline_length(durations, 1.0), 0)


class TestXfadeFilterGraph(unittest.TestCase):
    def _graph(self, durations, transition="fade"):
        return vt.build_xfade_filter(
            durations=durations,
            transition=transition,
            width=1080,
            height=1920,
            fps=30,
        )

    def test_each_input_is_normalised_before_the_chain(self):
        """
        xfade refuses inputs whose frame rate, pixel format or timebase differ,
        and the clips do not all come from the same place: most are written by
        _render_clip_with_ffmpeg, but a clip FFmpeg refused was written by
        MoviePy instead.
        """
        graph, _ = self._graph([5.0, 5.0])
        for index in (0, 1):
            self.assertIn(
                f"[{index}:v]fps=30,scale=1080:1920,setsar=1,"
                f"format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[cav{index}]",
                graph,
            )

    def test_the_offsets_are_positions_on_the_finished_timeline(self):
        """
        Not positions inside the clip being added. Each one is where the join
        so far ends, less the overlap - which is what puts the two clips on
        screen at the same time.
        """
        graph, output = self._graph([5.0, 5.0, 5.0])
        self.assertIn(
            "[cav0][cav1]xfade=transition=fade:duration=1.000:offset=4.000[caxf1]",
            graph,
        )
        self.assertIn(
            "[caxf1][cav2]xfade=transition=fade:duration=1.000:offset=8.000[caxf2]",
            graph,
        )
        self.assertEqual(output, "caxf2")

    def test_the_named_transition_reaches_every_join(self):
        graph, _ = self._graph([5.0, 5.0, 5.0], transition="circleclose")
        self.assertEqual(graph.count("transition=circleclose"), 2)

    def test_a_short_clip_shortens_its_own_join_only(self):
        graph, _ = self._graph([5.0, 1.0, 5.0])
        self.assertIn("duration=0.500:offset=4.500", graph)
        # 5.0 + 1.0 - 0.5 = 5.5 on the timeline, and the next join takes what
        # is left of the short clip rather than a full second.
        self.assertIn("duration=0.250:offset=5.250", graph)

    def test_the_last_label_is_the_one_to_map(self):
        for count in (2, 3, 7):
            with self.subTest(count=count):
                graph, output = self._graph([5.0] * count)
                self.assertEqual(output, f"caxf{count - 1}")
                self.assertIn(f"[{output}]", graph)

    def test_a_chain_needs_two_clips(self):
        with self.assertRaises(ValueError):
            self._graph([5.0])


if __name__ == "__main__":
    unittest.main()
