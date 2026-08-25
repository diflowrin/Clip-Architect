import { useEffect, useState } from 'react';

import type { BackendApi, VideoAspect, VideoParams } from '../lib/api';

/**
 * What the subtitles will look like, at the shape the video will be.
 *
 * Burned-in subtitles cannot be undone in the output, and the form used to ask
 * for a size in output pixels, two hex colours and an outline width without
 * rendering a single character. The feedback loop was: guess, wait six minutes,
 * open the file in a player, guess again.
 *
 * The chosen face is fetched from the backend and drawn with, so the frame is
 * faithful in letterform as well as in size, colour, outline and position. A
 * font the browser cannot load - a .ttc collection - falls back to a heavy
 * system sans, and says so, rather than showing a frame that quietly lies.
 */

/** Output width in pixels per aspect, which is what `font_size` is measured
 *  against. Scaling by it is what makes 60 look the same here as in the file. */
const OUTPUT_WIDTH: Record<VideoAspect, number> = {
  '9:16': 1080,
  '16:9': 1920,
  '1:1': 1080,
};

const RATIO: Record<VideoAspect, string> = {
  '9:16': '9 / 16',
  '16:9': '16 / 9',
  '1:1': '1 / 1',
};

interface Props {
  params: VideoParams;
  /** Real narration text when there is some; a stand-in otherwise. */
  sample?: string;
  /** Where the font file comes from. Optional so callers that only want the
   *  geometry - and the tests - need not wire a backend in. */
  api?: BackendApi;
}

/** Where the render breaks one subtitle from the next.
 *
 *  Mirrors `PUNCTUATIONS` in `app/models/const.py`, which is what
 *  `split_string_by_punctuations` walks the narration with. Kept as one string
 *  of characters rather than a regex of escapes so the two lists can be read
 *  against each other. */
const SUBTITLE_BREAKS = '?,.、;:!…？，。；：！،؛؟';

/** The first thing the viewer will read, or a stand-in before a script exists.
 *
 *  A decimal point or a thousands separator between digits is not a break -
 *  "2.5% fee" and "1,000 years" are one phrase - which is the same exemption
 *  the backend makes, and without it the preview would cut mid-number. */
function firstSubtitle(script: string): string {
  const source = script.trim();
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === '\n') break;
    if (!SUBTITLE_BREAKS.includes(char)) continue;
    if (
      (char === '.' || char === ',') &&
      /\d/.test(source[index - 1] ?? '') &&
      /\d/.test(source[index + 1] ?? '')
    ) {
      continue;
    }
    const phrase = source.slice(0, index).trim();
    if (phrase) return phrase;
  }
  return source || 'Your narration appears here';
}

/** The fallback stack, used before a face has loaded and for any the browser
 *  refuses. Heavy and condensed, because that is what burned-in subtitles are. */
const FALLBACK_STACK =
  "'Arial Black', 'Segoe UI', Roboto, system-ui, sans-serif";

/** Load the chosen font file and report the family name to draw with.
 *
 *  Fonts are registered under a name derived from the file, so switching the
 *  dropdown swaps the face rather than redefining one family and leaving the
 *  browser to serve whichever copy it cached first. Loads are not undone: the
 *  list is a handful of bundled files, they are what the picker cycles
 *  through, and dropping one on unmount would refetch it on the next render.
 */
function useSubtitleFont(fontName: string | undefined, api?: BackendApi) {
  const [loaded, setLoaded] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (!api || !fontName) return;
    // A collection cannot be loaded by a browser, so there is nothing to
    // fetch and the fallback is the honest answer.
    if (/\.ttc$/i.test(fontName)) return;
    if (loaded[fontName] !== undefined) return;

    let active = true;
    const family = familyFor(fontName);
    const face = new FontFace(family, `url("${api.fontFileUrl(fontName)}")`);
    face
      .load()
      .then((ready) => {
        if (!active) return;
        document.fonts.add(ready);
        setLoaded((previous) => ({ ...previous, [fontName]: true }));
      })
      .catch(() => {
        // A font the backend could not serve, or the browser could not parse.
        // The frame stays legible on the fallback and the caption says which.
        if (active) setLoaded((previous) => ({ ...previous, [fontName]: false }));
      });
    return () => {
      active = false;
    };
  }, [api, fontName, loaded]);

  const ready = !!fontName && loaded[fontName] === true;
  return {
    fontFamily: ready ? `'${familyFor(fontName)}', ${FALLBACK_STACK}` : FALLBACK_STACK,
    ready,
    // Undefined while the fetch is still in flight, so "still loading" and
    // "cannot be shown" are not reported as the same thing.
    failed: !!fontName && loaded[fontName] === false,
  };
}

/** A CSS family name per file, kept distinct so two faces never collide. */
function familyFor(fontName: string): string {
  return `ca-preview-${fontName.replace(/[^a-zA-Z0-9]+/g, '-')}`;
}

export function SubtitlePreview({ params, sample, api }: Props) {
  const aspect = params.video_aspect ?? '9:16';
  const outputWidth = OUTPUT_WIDTH[aspect];

  // Container query units, so the type scales with the frame instead of needing
  // a resize observer: 1cqw is one percent of the frame's own width, and the
  // output size is a percentage of the output's width.
  const fontSize = `${((params.font_size ?? 60) / outputWidth) * 100}cqw`;
  const strokeWidth = `${((params.stroke_width ?? 1.5) / outputWidth) * 100}cqw`;

  const position = params.subtitle_position ?? 'bottom';
  const justify =
    position === 'top' ? 'flex-start' : position === 'center' ? 'center' : 'flex-end';

  // The first subtitle, cut where the renderer cuts it. Splitting on sentence
  // ends alone was the reason a portrait preview filled half the frame with
  // nine lines of text: the pipeline breaks on every punctuation mark, commas
  // included, so a sentence with five clauses is five subtitles on screen -
  // never the wall this frame was drawing. A preview that overstates the text
  // is worse than none, because it sends people to shrink a font that was fine.
  const text = firstSubtitle(sample || params.video_script || '');

  const font = useSubtitleFont(params.font_name, api);

  const custom =
    position === 'custom'
      ? { paddingTop: `${params.custom_position ?? 70}%`, justifyContent: 'flex-start' as const }
      : {};

  return (
    <div className="subtitle-preview" style={{ aspectRatio: RATIO[aspect] }}>
      <div className="subtitle-preview__scene" aria-hidden="true" />
      <div
        className="subtitle-preview__stage"
        style={{ justifyContent: justify, ...custom }}
      >
        {params.subtitle_enabled === false ? null : (
          <span
            className="subtitle-preview__text"
            style={{
              fontSize,
              fontFamily: font.fontFamily,
              // The bundled faces carry their own weight in the file - the
              // list is all "-Bold" and "-Black". Forcing 800 on top of one
              // makes the browser synthesise a second bolding the render will
              // not apply, which is exactly the kind of lie this frame exists
              // to prevent. The fallback still needs it.
              fontWeight: font.ready ? 'normal' : 800,
              color: params.text_fore_color ?? '#FFFFFF',
              WebkitTextStrokeWidth: strokeWidth,
              WebkitTextStrokeColor: params.stroke_color ?? '#000000',
            }}
          >
            {text}
          </span>
        )}
      </div>
    </div>
  );
}
