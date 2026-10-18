"""
Real transitions between clips, made with FFmpeg's ``xfade`` filter.

A transition is something that happens *between* two shots. What this app had
instead was an effect applied inside one clip, with the clips then joined by
the concat demuxer - which never puts two of them on screen at the same time.
So "FadeIn" faded up from black at every cut, "SlideIn" slid over black rather
than over the shot before it, and the result read as a slideshow with a black
frame punched between the slides. ``xfade`` overlaps the two clips instead,
which is what every editor means by the word.

The overlap is not free: a one-second transition at each of twelve joins takes
twelve seconds off the finished length, so the cut has to be built longer to
compensate. ``timeline_length`` is what the caller measures itself against, and
``build_xfade_filter`` builds the graph from the same numbers, so the estimate
and the render cannot drift apart.
"""

import random
import subprocess
from functools import lru_cache

from loguru import logger

from app.models.schema import VideoTransitionMode

# How much of the timeline one transition eats. The old per-clip effects were
# all written with t=1, and a second is the usual length for a cut between
# shots of five to fifteen seconds.
TRANSITION_DURATION = 1.0

# What every mode falls back to. `fade` is a plain cross-dissolve and has been
# in xfade since the filter was added, so it is the one name that can be
# assumed present once the filter itself is.
_DEFAULT_XFADE = "fade"

# Each mode, in the order its xfade names are preferred. The app ships FFmpeg
# n9.0, where all of these exist, but `get_ffmpeg_binary` prefers an ffmpeg
# already on PATH - which can be years older. The later entries are the ones
# that were in xfade from the start.
#
# The names were picked by rendering each candidate and looking at the frames,
# not from the filter list:
#
# - `zoomin` magnifies the outgoing shot until it fills the frame and hands
#   over at the top of the zoom, so it reads as a punch in.
# - There is no `zoomout`. `circleclose` is the closest thing that still reads
#   as the outgoing shot receding: it closes to a point with the incoming shot
#   already behind it. `rectcrop` shrinks the frame more literally but passes
#   through a fully black frame at the halfway point, which is exactly the
#   flash this module exists to remove.
# - `coverleft` slides the incoming shot in over the outgoing one; `revealleft`
#   slides the outgoing one away to uncover it. That pairing is what "slide in"
#   and "slide out" described when they were per-clip effects, so the two
#   settings stay distinguishable instead of both becoming a generic slide.
_XFADE_PREFERENCES: dict[str, tuple[str, ...]] = {
    VideoTransitionMode.fade_in.value: ("fade",),
    # The old FadeOut dipped to black at the end of every clip. `fadeblack`
    # keeps that look and makes it one dip at the cut rather than two.
    VideoTransitionMode.fade_out.value: ("fadeblack", "fade"),
    VideoTransitionMode.slide_in.value: ("coverleft", "slideleft", "fade"),
    VideoTransitionMode.slide_out.value: ("revealleft", "slideright", "fade"),
    VideoTransitionMode.zoom_in.value: ("zoomin", "circlecrop", "fade"),
    VideoTransitionMode.zoom_out.value: ("circleclose", "fade"),
}

# What Shuffle draws from. It used to draw once per clip, which is why it read
# as a mistake rather than a style: three cuts into the same video the viewer
# has seen a dissolve, a slide and a zoom and cannot tell what the next one
# will be. One draw per render keeps the surprise and drops the inconsistency.
_SHUFFLE_MODES: tuple[str, ...] = tuple(_XFADE_PREFERENCES)


def _is_no_transition(mode) -> bool:
    value = getattr(mode, "value", mode)
    return value in (None, "", VideoTransitionMode.none.value)


@lru_cache(maxsize=4)
def supported_transitions(ffmpeg_binary: str) -> frozenset[str]:
    """
    The transition names this FFmpeg build's ``xfade`` accepts.

    Read from the filter's own help output rather than assumed from a version
    number: a name that is not there fails the whole render at the last step,
    after every clip has already been encoded.
    """
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-h", "filter=xfade"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"failed to inspect ffmpeg xfade transitions: {str(exc)}")
        return frozenset()

    if result.returncode != 0:
        logger.warning(
            "failed to inspect ffmpeg xfade transitions: "
            f"{(result.stderr or result.stdout or '').strip()[:200]}"
        )
        return frozenset()

    output = result.stdout or ""
    if "AVOptions" not in output:
        # An FFmpeg without xfade answers "Unknown filter" on stdout with a
        # zero exit code, so the return code alone does not settle it.
        return frozenset()

    names = set()
    for line in output.splitlines():
        # The option list indents each value as "     fade   0   ..FV....... ".
        parts = line.split()
        if len(parts) >= 3 and line.startswith("     ") and parts[1].lstrip("-").isdigit():
            names.add(parts[0])
    return frozenset(names)


def resolve_transition(mode, ffmpeg_binary: str) -> str | None:
    """
    The xfade transition to use for a whole render, or None for hard cuts.

    Called once per render, which is the point: Shuffle draws here, so every
    cut in one video gets the same treatment.
    """
    if _is_no_transition(mode):
        return None

    value = getattr(mode, "value", mode)
    if value == VideoTransitionMode.shuffle.value:
        value = random.choice(_SHUFFLE_MODES)
        logger.info(f"shuffle picked the {value} transition for this render")

    available = supported_transitions(ffmpeg_binary)
    if not available:
        logger.warning(
            "this ffmpeg build has no xfade transitions, joining clips with "
            "hard cuts instead"
        )
        return None

    for candidate in _XFADE_PREFERENCES.get(value, (_DEFAULT_XFADE,)):
        if candidate in available:
            return candidate

    if _DEFAULT_XFADE in available:
        logger.warning(
            f"this ffmpeg build has no transition for {value}, "
            f"using {_DEFAULT_XFADE}"
        )
        return _DEFAULT_XFADE

    logger.warning(f"no usable xfade transition for {value}, using hard cuts")
    return None


def overlaps_for(
    durations: list[float],
    requested: float = TRANSITION_DURATION,
) -> list[float]:
    """
    How much of the timeline each join actually takes, one entry per join.

    Rarely the full second. A transition may never take more than half of
    either shot it joins - a one-second dissolve across a 1.2-second tail would
    otherwise leave nothing of the tail to see - and the outgoing clip is
    measured by what its own incoming transition left of it, so two short clips
    in a row cannot be consumed twice over.
    """
    if requested <= 0 or len(durations) < 2:
        return []

    overlaps: list[float] = []
    available_previous = max(float(durations[0]), 0.0)
    for duration in durations[1:]:
        duration = max(float(duration), 0.0)
        overlap = min(requested, available_previous / 2, duration / 2)
        overlap = max(overlap, 0.0)
        overlaps.append(overlap)
        available_previous = duration - overlap
    return overlaps


def timeline_length(
    durations: list[float],
    requested: float = TRANSITION_DURATION,
) -> float:
    """
    How long these clips run once joined, overlaps included.

    The cut is built against this rather than the sum of the clips: with
    transitions on, the sum is longer than the video and the render would stop
    collecting material before it covered the narration.
    """
    total = sum(max(float(duration), 0.0) for duration in durations)
    return max(total - sum(overlaps_for(durations, requested)), 0.0)


def build_xfade_filter(
    durations: list[float],
    transition: str,
    width: int,
    height: int,
    fps: int,
    requested: float = TRANSITION_DURATION,
) -> tuple[str, str]:
    """
    Build the ``-filter_complex`` graph that joins the clips with overlaps.

    Returns the graph and the label to map as the output.

    Each input is normalised first. xfade refuses inputs whose frame rate,
    pixel format or timebase differ, and the clips do not all come from the
    same place: most are written by ``_render_clip_with_ffmpeg``, but any clip
    FFmpeg refused was written by MoviePy instead. The normalisation is a
    pass-through for everything already in the right shape.
    """
    if len(durations) < 2:
        raise ValueError("an xfade chain needs at least two clips")

    overlaps = overlaps_for(durations, requested)
    parts = [
        f"[{index}:v]fps={fps},scale={width}:{height},setsar=1,"
        f"format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[cav{index}]"
        for index in range(len(durations))
    ]

    previous_label = "cav0"
    # Where the running join ends on the finished timeline. The next clip is
    # laid down `overlap` seconds before that, which is what makes the two
    # overlap at all - offset is measured from the start of the chain so far,
    # not from the start of the clip being added.
    timeline = max(float(durations[0]), 0.0)
    for index, overlap in enumerate(overlaps, start=1):
        offset = max(timeline - overlap, 0.0)
        label = f"caxf{index}"
        parts.append(
            f"[{previous_label}][cav{index}]xfade=transition={transition}:"
            f"duration={overlap:.3f}:offset={offset:.3f}[{label}]"
        )
        timeline = timeline + max(float(durations[index]), 0.0) - overlap
        previous_label = label

    return ";".join(parts), previous_label
