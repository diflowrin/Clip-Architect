"""
Build an ASS subtitle file that draws what the MoviePy subtitle pass draws.

Burning subtitles used to mean compositing every frame of the video in Python:
one ``TextClip`` per subtitle, a ``CompositeVideoClip`` over the whole film, and
a MoviePy write. That runs for minutes on a video of a few seconds. libass
draws the same text inside FFmpeg's own filter graph, which costs nothing
measurable next to the encode that has to happen anyway.

This module only produces the ``.ass`` file. Deciding whether the fast path can
be used at all, and running FFmpeg, belong to ``video.py``.

Everything here is written against the styling the form already offers, so a
project rendered before this existed keeps its look: the same font, size,
colour, outline, background, wrapping and vertical placement. Where ASS cannot
express a setting - a rounded background box is the one case - the caller is
told, and falls back to the old path rather than quietly drawing something
else.
"""

import os
import re
import struct
from typing import NamedTuple

from loguru import logger
from PIL import ImageFont

# ASS timestamps are h:mm:ss.cc - centiseconds, and a single-digit hour.
_SRT_TIME = re.compile(
    r"(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)

# The subtitle block is drawn inside 90% of the frame width, the same as
# `max_width = video_width * 0.9` in the MoviePy path.
_HORIZONTAL_MARGIN_RATIO = 0.05

# Where "bottom" and "top" sit, as a fraction of frame height. The MoviePy path
# places the text block's bottom edge at 95% of the height and its top edge at
# 5%, so the distance from the matching frame edge is the same 5% either way.
_VERTICAL_MARGIN_RATIO = 0.05

# ASS alignment values (numpad layout).
_ALIGN_BOTTOM_CENTER = 2
_ALIGN_MIDDLE_CENTER = 5
_ALIGN_TOP_CENTER = 8

# BorderStyle 1 draws an outline; 4 draws a box behind the line *and* keeps the
# outline, which is what the form's "background colour" plus stroke asks for.
# (3 would replace the outline with the box, losing the stroke.)
_BORDER_OUTLINE = 1
_BORDER_BOX = 4

# WrapStyle 2 disables libass's own line breaking. The text arrives already
# wrapped by `video.wrap_text`, so the lines are exactly the ones the MoviePy
# path measured - and knowing the line count is what lets a custom vertical
# position be placed where it used to be.
_WRAP_NONE = 2


class SubtitleEvent(NamedTuple):
    """
    One subtitle, measured the way the MoviePy path measured it.

    ``text`` arrives already wrapped - newlines are the line breaks
    ``video.wrap_text`` chose - so libass draws the same lines rather than
    breaking the text somewhere else of its own accord.

    ``text_height`` is how tall the drawn glyphs are and ``block_height`` how
    tall the box was that the old path drew them inside. Half the difference
    between them sat under the text, and a subtitle placed without accounting
    for it lands that much off. Both are per subtitle because a two-line one
    has more padding than a one-line one.
    """

    start: float
    end: float
    text: str
    text_height: int
    block_height: int
    # How far the bottom line's glyphs reach below their baseline. libass
    # anchors a line by its font's box, not by the ink in it, so this is what
    # turns "where the box goes" into "where the letters land".
    descender: int = 0


def supports(params) -> tuple[bool, str]:
    """
    Whether this styling can be drawn by libass.

    Returns the answer and, when it is no, a reason worth logging. A rounded
    background is a rounded rectangle composited under the text; ASS boxes are
    square, so drawing one here would silently change how finished videos look.
    """
    background = getattr(params, "text_background_color", False)
    rounded = bool(getattr(params, "rounded_subtitle_background", False))
    if rounded and background:
        return False, "the subtitle background is rounded, which ASS cannot draw"
    return True, ""


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        raise ValueError(f"not a hex colour: {color!r}")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def ass_color(color: str, alpha: int = 0, default: str = "#FFFFFF") -> str:
    """
    Convert ``#RRGGBB`` to the ``&HAABBGGRR`` ASS literal.

    Two traps live in that format: the channels are reversed, and the alpha
    byte is *transparency* - 00 is opaque and FF is invisible, the opposite of
    every alpha the rest of this codebase uses. ``alpha`` here is stated the
    ASS way so the value written is the value read.
    """
    try:
        red, green, blue = _hex_to_rgb(color)
    except (ValueError, TypeError):
        # A malformed colour is not worth failing a render over; the form's
        # own default is a better answer than a crash.
        logger.warning(f"unusable subtitle colour {color!r}, using {default}")
        red, green, blue = _hex_to_rgb(default)
    return f"&H{alpha & 0xFF:02X}{blue:02X}{green:02X}{red:02X}"


def font_family(font_path: str) -> tuple[str, bool]:
    """
    Resolve the family name and weight libass should be asked for.

    ASS names a font by family, not by file: ``Charm-Bold.ttf`` has to be
    requested as "Charm" with the bold flag set. Reading it from the file
    rather than parsing the filename means a font whose name and filename
    disagree still resolves.
    """
    fallback = os.path.splitext(os.path.basename(font_path or ""))[0] or "Arial"
    try:
        family, style = ImageFont.truetype(font_path, 40).getname()
    except Exception as exc:
        logger.warning(
            f"cannot read the family name of {font_path!r} ({str(exc)}), "
            f"asking libass for {fallback!r}"
        )
        return fallback, False
    return (family or fallback), ("bold" in (style or "").lower())


def _read_win_metrics(font_path: str) -> tuple[int, int, int] | None:
    """
    Read ``unitsPerEm`` and the OS/2 Windows ascent/descent from a font file.

    Parsed here rather than through a library because it is three numbers out
    of two tables, and the alternative is shipping fontTools inside the desktop
    bundle for them. Returns None when the file is not a font this can read, so
    the caller can fall back rather than fail.
    """
    try:
        with open(font_path, "rb") as handle:
            data = handle.read()
        table_count = struct.unpack(">H", data[4:6])[0]
        tables: dict[str, bytes] = {}
        for index in range(table_count):
            entry = 12 + index * 16
            tag = data[entry : entry + 4].decode("latin-1")
            offset, length = struct.unpack(">II", data[entry + 8 : entry + 16])
            tables[tag] = data[offset : offset + length]
        units_per_em = struct.unpack(">H", tables["head"][18:20])[0]
        win_ascent, win_descent = struct.unpack(">HH", tables["OS/2"][74:78])
    except Exception as exc:
        logger.warning(f"cannot read font metrics from {font_path!r}: {str(exc)}")
        return None
    if units_per_em <= 0 or (win_ascent + win_descent) <= 0:
        return None
    return units_per_em, win_ascent, win_descent


def ass_font_size(font_path: str, pixel_size: int) -> int:
    """
    Convert a pixel font size into the number ASS calls ``Fontsize``.

    They are not the same number, and the difference is large enough to be the
    whole ballgame: the form's 64 drew Charm at 64 pixels through Pillow but at
    27 through libass - text less than half the size it used to be. ASS follows
    GDI, where the size names the *font's total line box* (OS/2 usWinAscent +
    usWinDescent), not its em square. The ratio is per font, so it cannot be a
    constant: 2.35 for Charm, 1.52 for Noto Sans.

    Measured against Pillow across three shipped fonts and three sizes, this
    lands within about one percent - the residue being integer rounding of a
    value ASS states as a whole number.
    """
    metrics = _read_win_metrics(font_path)
    if metrics is None:
        # Better a subtitle at the wrong size than no video: the em size is
        # what a reader would guess, and it is only wrong by the ratio above.
        return max(1, int(round(pixel_size)))
    units_per_em, win_ascent, win_descent = metrics
    return max(1, int(round(pixel_size * (win_ascent + win_descent) / units_per_em)))


def _format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, rest = divmod(rest, 60)
    whole = int(rest)
    centiseconds = int(round((rest - whole) * 100))
    if centiseconds == 100:
        whole += 1
        centiseconds = 0
    return f"{int(hours)}:{int(minutes):02d}:{whole:02d}.{centiseconds:02d}"


def _escape_text(text: str) -> str:
    """
    Make one subtitle safe to put in a Dialogue line.

    Braces open an ASS override block and a newline ends the event, so text
    carrying either would be read as markup rather than drawn.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\N")
    )


def parse_srt_time(value: str) -> float:
    match = _SRT_TIME.search(value or "")
    if not match:
        raise ValueError(f"not an SRT timestamp: {value!r}")
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(match.group("ms").ljust(3, "0")) / 1000.0
    )


def line_box_descent(font_path: str, ass_size: int) -> int:
    """
    How far below the baseline libass's line box reaches, in pixels.

    ``Fontsize`` spans the font's whole Windows line box, so the box is split
    between ascent and descent in the same proportion the OS/2 table states.
    Subtitles are anchored on that box; the glyphs sit somewhere inside it.
    """
    metrics = _read_win_metrics(font_path)
    if metrics is None:
        return 0
    _units_per_em, win_ascent, win_descent = metrics
    return int(round(ass_size * win_descent / (win_ascent + win_descent)))


def _vertical_margin(
    event: SubtitleEvent,
    position: str,
    video_height: int,
    custom_position: float = 70.0,
    box_descent: int = 0,
) -> tuple[int, int]:
    """
    Where one subtitle sits: its ASS alignment, and its distance from the edge.

    Reproduces the placement arithmetic of the MoviePy path, which positioned
    the *box* it drew and left the glyphs floating in the middle of it. ASS
    positions the glyphs, so the half of the padding that sat under them is
    added back here.
    """
    padding_below_text = max(
        0, int(round((event.block_height - event.text_height) / 2))
    )
    edge = int(round(video_height * _VERTICAL_MARGIN_RATIO))
    # The margin names the line box; the eye reads the ink. Shifting by the
    # gap between the two is what puts the letters where they used to be
    # rather than a font-dependent distance above it.
    ink_correction = box_descent - int(event.descender)

    if position == "top":
        return _ALIGN_TOP_CENTER, edge + padding_below_text
    if position == "center":
        # A centred alignment ignores the margin, and a stated zero is clearer
        # than leaving whatever another branch computed.
        return _ALIGN_MIDDLE_CENTER, 0
    if position == "custom":
        # Measured from the top of the block, clamped so the block stays on
        # screen - the same arithmetic, against the same numbers, as before.
        margin = 10
        top = (video_height - event.block_height) * (float(custom_position) / 100.0)
        top = max(margin, min(top, video_height - event.block_height - margin))
        return _ALIGN_TOP_CENTER, int(round(top)) + padding_below_text
    return _ALIGN_BOTTOM_CENTER, edge + padding_below_text - ink_correction


def build(
    events: list[SubtitleEvent],
    output_path: str,
    *,
    params,
    video_width: int,
    video_height: int,
    font_path: str,
) -> str:
    """Write the ``.ass`` file and return its path."""
    family, bold = font_family(font_path)
    font_size = ass_font_size(font_path, int(params.font_size))
    outline = float(params.stroke_width or 0)

    background = getattr(params, "text_background_color", False)
    if isinstance(background, bool):
        background_color = "#000000" if background else None
    else:
        background_color = background or None

    border_style = _BORDER_BOX if background_color else _BORDER_OUTLINE
    # The MoviePy path draws its square background fully opaque.
    back_colour = ass_color(background_color or "#000000", alpha=0x00)

    horizontal_margin = int(round(video_width * _HORIZONTAL_MARGIN_RATIO))
    position = (getattr(params, "subtitle_position", "") or "bottom").lower()
    custom_position = float(getattr(params, "custom_position", 70.0) or 0.0)

    # The style carries a placement so a malformed event still lands somewhere
    # sensible; every event then states its own, because the padding to correct
    # for depends on how many lines that particular subtitle wrapped to.
    style_alignment = {
        "top": _ALIGN_TOP_CENTER,
        "center": _ALIGN_MIDDLE_CENTER,
        "custom": _ALIGN_TOP_CENTER,
    }.get(position, _ALIGN_BOTTOM_CENTER)
    style_margin = int(round(video_height * _VERTICAL_MARGIN_RATIO))
    box_descent = line_box_descent(font_path, font_size)

    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            # Sizes and margins below are in pixels of the real frame, which is
            # only true while the declared resolution matches it.
            f"PlayResX: {int(video_width)}",
            f"PlayResY: {int(video_height)}",
            f"WrapStyle: {_WRAP_NONE}",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: None",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
            " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
            " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
            " Alignment, MarginL, MarginR, MarginV, Encoding",
            ",".join(
                [
                    "Style: Default",
                    family,
                    str(font_size),
                    ass_color(params.text_fore_color, default="#FFFFFF"),
                    ass_color(params.text_fore_color, default="#FFFFFF"),
                    ass_color(params.stroke_color, default="#000000"),
                    back_colour,
                    "-1" if bold else "0",
                    "0",
                    "0",
                    "0",
                    "100",
                    "100",
                    "0",
                    "0",
                    str(border_style),
                    f"{outline:g}",
                    "0",
                    str(style_alignment),
                    str(horizontal_margin),
                    str(horizontal_margin),
                    str(max(0, style_margin)),
                    "1",
                ]
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR,"
            " MarginV, Effect, Text",
        ]
    )

    lines = [header]
    for event in events:
        if event.end <= event.start:
            # A zero-length event draws nothing and confuses libass's ordering.
            continue
        alignment, vertical_margin = _vertical_margin(
            event, position, video_height, custom_position, box_descent
        )
        # An event cannot state an alignment in its margin fields, so a
        # per-event override rides in as an inline tag ahead of the text.
        prefix = (
            "{" + chr(92) + "an" + str(alignment) + "}"
            if alignment != style_alignment
            else ""
        )
        lines.append(
            "Dialogue: 0,{start},{end},Default,,0,0,{margin},,{prefix}{text}".format(
                start=_format_timestamp(event.start),
                end=_format_timestamp(event.end),
                margin=max(0, vertical_margin),
                prefix=prefix,
                text=_escape_text(event.text),
            )
        )

    with open(output_path, "w", encoding="utf-8-sig") as handle:
        handle.write("\n".join(lines) + "\n")

    logger.info(
        f"wrote {len(lines) - 1} subtitle event(s) to {os.path.basename(output_path)}: "
        f"font={family!r} size={font_size} bold={bold} position={position!r}"
    )
    return output_path
