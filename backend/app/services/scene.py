"""
How long a scene runs, and how many clips a video is allowed to download.

One constant used to answer both questions. Every scene was
``video_clip_duration`` seconds long - five by default - and the download loop
fetched one fresh clip per scene until it had covered the narration. A
sixty-nine second narration therefore pulled fifteen separate stock clips: one
download per five seconds of finished video, hundreds of megabytes for a video
nobody had watched yet.

Scenes of a single length also read as a metronome. Real edits hold some shots
and cut others quickly, so the default here is a varied plan: each scene draws
five, ten or fifteen seconds. The average of ten halves how many clips a video
needs, and gives the cut a rhythm a fixed length cannot.

``video_clip_duration = 0`` means varied. Any positive value pins every scene to
exactly that length, which is what the fixed choice in the form writes and what
every existing API caller already sends - so their renders are unchanged.

The clip ceiling is separate from the plan and deliberately blunt: an unlucky
run of five-second draws must not turn back into fifteen downloads. Whatever
the plan does, a video may not fetch more than
``MAX_CLIPS_PER_MINUTE`` clips per minute of footage. Reaching the ceiling
costs variety, never length - the renderer cuts more than one scene out of a
long clip when it has to.
"""

import math
import random

# The sentinel `video_clip_duration` value meaning "vary the scene length".
VARIED_SCENE_LENGTH = 0

# What a varied plan draws from, in seconds. Five is the shortest scene that
# still registers as a shot rather than a flash; fifteen is about as long as
# stock footage holds attention without a cut.
SCENE_DURATION_CHOICES: tuple[int, ...] = (5, 10, 15)

# The download ceiling, per minute of finished video. Seven clips a minute is
# roughly one clip every eight and a half seconds - below the ten-second
# average a varied plan aims for, so the ceiling only ever catches the unlucky
# runs.
MAX_CLIPS_PER_MINUTE = 7

# Even a five-second video gets a cut in it. One clip would make the ceiling,
# not the plan, decide what the video looks like.
_MIN_CLIP_COUNT = 2


def is_varied(clip_duration: int | float | None) -> bool:
    """Whether this setting asks for varied scene lengths."""
    try:
        return int(clip_duration or VARIED_SCENE_LENGTH) <= 0
    except (TypeError, ValueError):
        # A malformed setting is not a reason to fail a render; the varied
        # plan is the default the form writes anyway.
        return True


def _fixed_duration(clip_duration: int | float) -> int:
    return max(1, int(clip_duration))


def next_scene_duration(
    clip_duration: int | float | None,
    remaining: float | None = None,
    rng: random.Random | None = None,
) -> int:
    """
    How long the next scene should run, in seconds.

    ``remaining`` is how much of the video is still uncovered. Scenes that fit
    inside it are all fair game, and so is the shortest one that runs past it -
    a plan with eight seconds left should be able to finish in one ten-second
    scene rather than being forced into two five-second ones. Without that, the
    tail of every video collapses to the shortest choice and the clip count
    creeps back up.
    """
    if not is_varied(clip_duration):
        return _fixed_duration(clip_duration)

    if remaining is None:
        # The render path slices whatever footage it has and lets the cut end
        # where the audio does; there is no remainder to fit into.
        return (rng or random).choice(list(SCENE_DURATION_CHOICES))

    choices = [choice for choice in SCENE_DURATION_CHOICES if choice <= remaining]
    overshoot = [choice for choice in SCENE_DURATION_CHOICES if choice > remaining]
    if overshoot:
        choices.append(min(overshoot))
    return (rng or random).choice(choices)


def longest_scene_duration(clip_duration: int | float | None) -> int:
    """
    The most any single scene may run.

    The render path needs one number rather than a draw: it caps each cut clip
    after the fact, and a cap below the scene it was handed would silently trim
    the plan back to the old fixed length.
    """
    if not is_varied(clip_duration):
        return _fixed_duration(clip_duration)
    return max(SCENE_DURATION_CHOICES)


def minimum_material_duration(clip_duration: int | float | None) -> int:
    """
    The shortest source clip worth searching for.

    Searching for footage as long as the longest scene would throw away most of
    a provider's catalogue for the sake of the occasional fifteen-second shot.
    A clip shorter than the scene it lands in simply makes that scene shorter,
    which the download accounting already handles by measuring what it got.
    """
    if not is_varied(clip_duration):
        return _fixed_duration(clip_duration)
    return min(SCENE_DURATION_CHOICES)


def still_image_duration(clip_duration: int | float | None) -> int:
    """How long a still image runs when it is turned into a clip."""
    if not is_varied(clip_duration):
        return _fixed_duration(clip_duration)
    return min(SCENE_DURATION_CHOICES)


def max_material_count(
    target_duration: float,
    clip_duration: int | float | None = VARIED_SCENE_LENGTH,
) -> int:
    """
    How many separate clips a video of this length may download.

    Proportional rather than absolute: a minute may fetch seven, so five
    minutes may fetch thirty-five. Nothing here decides how long the video is -
    running out of clips only means the renderer takes more than one scene from
    the footage it already has.

    A pinned scene length is not overruled. Someone who asks for five-second
    scenes has asked for twelve scenes a minute, and answering with seven would
    be this ceiling deciding what their video looks like. The ceiling exists for
    the varied default, where the plan aims at ten-second scenes and only an
    unlucky run of short draws would push the count back up.
    """
    try:
        seconds = float(target_duration)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        return _MIN_CLIP_COUNT

    ceiling = math.ceil(seconds * MAX_CLIPS_PER_MINUTE / 60)
    if not is_varied(clip_duration):
        # One clip per scene, plus the one that carries the total past the
        # target - the same count the loop reached before there was a ceiling.
        ceiling = max(ceiling, math.ceil(seconds / _fixed_duration(clip_duration)) + 1)
    return max(_MIN_CLIP_COUNT, ceiling)
