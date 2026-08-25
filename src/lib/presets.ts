/**
 * Presets and subtitle styles: the two mechanisms that take thirty-six visible
 * controls down to six.
 *
 * Neither is a new capability. Every value below is one the form already
 * exposed as its own control; grouping them is what makes the screen readable
 * by someone who has no mental model of the pipeline. A preset answers "what
 * kind of video is this" once, instead of asking eleven questions whose answers
 * always travel together.
 *
 * User presets live in local storage rather than on the server, because the
 * settings are the user's own input and never needed a round trip - the same
 * reasoning as `lastRender`.
 */

import type { VideoParams } from './api';

const USER_PRESETS_KEY = 'cliparchitect.presets.v1';

export interface Preset {
  id: string;
  label: string;
  /** One line, in the user's terms, not the pipeline's. */
  hint: string;
  /** Only the fields this preset decides. Everything else is left alone. */
  params: Partial<VideoParams>;
  builtIn: boolean;
}

/**
 * The four shipped presets.
 *
 * The first needs no account of any kind: local files are not required, but
 * Edge TTS speaks for free, and a person who has not set up a stock-footage
 * key can still reach a finished video through it. That is the first-run route
 * the Store reviewer has to find, so it leads.
 *
 * None of them names a voice. They used to pin `voice_name: ''`, which decided
 * nothing - the voice list fills that field the moment it loads - but did
 * break `matchPreset` for good: on a fresh install the chip lit up on the
 * first paint and went dark a moment later, and the hint then told a
 * brand-new user their untouched defaults were "your own settings". A preset
 * that clears the chosen voice would also be wrong on its own terms: switching
 * from landscape to square is not a request to change who is speaking.
 */
export const BUILT_IN_PRESETS: Preset[] = [
  {
    id: 'vertical-narrated',
    label: 'Vertical, narrated',
    hint: 'TikTok, Reels and Shorts. A free voice reads the script.',
    params: {
      video_aspect: '9:16',
      voice_enabled: true,
      voice_rate: 1,
      voice_volume: 1,
      subtitle_enabled: true,
      bgm_type: 'random',
      bgm_volume: 0.2,
      video_transition_mode: null,
    },
    builtIn: true,
  },
  {
    id: 'vertical-music',
    label: 'Vertical, music only',
    hint: 'No voice and no subtitles. The clips carry it.',
    params: {
      video_aspect: '9:16',
      voice_enabled: false,
      subtitle_enabled: false,
      bgm_type: 'random',
      bgm_volume: 0.6,
      video_transition_mode: 'FadeIn',
    },
    builtIn: true,
  },
  {
    id: 'landscape-narrated',
    label: 'Landscape, narrated',
    hint: 'YouTube and anything watched on a wide screen.',
    params: {
      video_aspect: '16:9',
      voice_enabled: true,
      subtitle_enabled: true,
      bgm_type: 'random',
      bgm_volume: 0.2,
      video_transition_mode: null,
    },
    builtIn: true,
  },
  {
    id: 'square-narrated',
    label: 'Square, narrated',
    hint: 'Feed posts that should not be cropped either way.',
    params: {
      video_aspect: '1:1',
      voice_enabled: true,
      subtitle_enabled: true,
      bgm_type: 'random',
      bgm_volume: 0.2,
      video_transition_mode: null,
    },
    builtIn: true,
  },
];

/**
 * A named subtitle look, replacing six numeric controls with one choice.
 *
 * Burned-in subtitles are irreversible in the output, and the old form asked
 * for a font file name, a pixel size, two hex colours and an outline width
 * without rendering a single character. These are the same values, named for
 * what they do and shown before they cost a render.
 */
export interface SubtitleStyle {
  id: string;
  label: string;
  params: Pick<
    VideoParams,
    'font_size' | 'text_fore_color' | 'stroke_color' | 'stroke_width' | 'subtitle_position'
  >;
}

export const SUBTITLE_STYLES: SubtitleStyle[] = [
  {
    id: 'clean',
    label: 'Clean',
    params: {
      font_size: 60,
      text_fore_color: '#FFFFFF',
      stroke_color: '#000000',
      stroke_width: 1.5,
      subtitle_position: 'bottom',
    },
  },
  {
    id: 'heavy',
    label: 'Heavy',
    params: {
      font_size: 74,
      text_fore_color: '#FFFFFF',
      stroke_color: '#000000',
      stroke_width: 3,
      subtitle_position: 'bottom',
    },
  },
  {
    id: 'highlight',
    label: 'Highlight',
    params: {
      font_size: 64,
      text_fore_color: '#FFE44D',
      stroke_color: '#000000',
      stroke_width: 2.5,
      subtitle_position: 'bottom',
    },
  },
  {
    id: 'quiet',
    label: 'Quiet',
    params: {
      font_size: 46,
      text_fore_color: '#FFFFFF',
      stroke_color: '#000000',
      stroke_width: 1,
      subtitle_position: 'bottom',
    },
  },
];

/** Which style a set of params matches, or null when the user has tuned the
 *  values by hand in Advanced. Compared on the fields a style owns, so a
 *  different font or position does not falsely un-match. */
export function matchSubtitleStyle(params: VideoParams): string | null {
  const found = SUBTITLE_STYLES.find(
    (style) =>
      style.params.font_size === params.font_size &&
      style.params.text_fore_color === params.text_fore_color &&
      style.params.stroke_color === params.stroke_color &&
      style.params.stroke_width === params.stroke_width,
  );
  return found?.id ?? null;
}

/** Which preset a set of params matches, on the fields that preset decides. */
export function matchPreset(params: VideoParams, presets: Preset[]): string | null {
  const found = presets.find((preset) =>
    (Object.keys(preset.params) as Array<keyof VideoParams>).every(
      (key) => params[key] === preset.params[key],
    ),
  );
  return found?.id ?? null;
}

/**
 * Script length as the user thinks of it.
 *
 * The underlying control was "Paragraphs", which is a fact about the prompt
 * rather than about the video. Nobody wants three paragraphs; they want a short
 * video. The mapping is the translation.
 */
export const SCRIPT_LENGTHS: Array<{
  id: string;
  label: string;
  paragraphs: number;
  /** What the video should run to, in seconds. */
  seconds: number;
  hint: string;
}> = [
  { id: 'short', label: 'Short', paragraphs: 1, seconds: 20, hint: 'Around 20 seconds.' },
  { id: 'medium', label: 'Medium', paragraphs: 2, seconds: 40, hint: 'Around 40 seconds.' },
  { id: 'long', label: 'Long', paragraphs: 4, seconds: 75, hint: 'A minute and a bit.' },
];

/** The length that is whatever the user typed. */
export const CUSTOM_LENGTH_ID = 'custom';

/** How much narration fits in a second, at the default voice rate. Mirrors
 *  `NARRATION_WORDS_PER_SECOND` in the backend, which is what actually
 *  budgets the script; this copy only exists to say so on screen. */
export const NARRATION_WORDS_PER_SECOND = 2.5;

/** Roughly how many words of narration a length asks for. */
export function wordsForSeconds(seconds: number): number {
  return Math.max(10, Math.round(seconds * NARRATION_WORDS_PER_SECOND));
}

/**
 * Paragraphs for a length nobody picked off the chips.
 *
 * The model is told both, and they have to agree: asking for four paragraphs
 * in twenty seconds produces four sentences that call themselves paragraphs.
 * One per twenty-five seconds is the ratio the three named lengths use.
 */
export function paragraphsForSeconds(seconds: number): number {
  return Math.min(10, Math.max(1, Math.round(seconds / 25)));
}

/** Which length chip a set of params is on, or `custom` for anything else. */
export function lengthIdForParams(params: {
  paragraph_number?: number;
  video_duration?: number;
}): string {
  const seconds = params.video_duration ?? 0;
  const paragraphs = params.paragraph_number ?? 1;
  const match = SCRIPT_LENGTHS.find(
    (option) => option.seconds === seconds && option.paragraphs === paragraphs,
  );
  return match?.id ?? CUSTOM_LENGTH_ID;
}

// --- user presets -----------------------------------------------------------

export function readUserPresets(): Preset[] {
  try {
    const raw = window.localStorage.getItem(USER_PRESETS_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (entry): entry is Preset =>
        !!entry && typeof entry === 'object' && typeof (entry as Preset).id === 'string',
    );
  } catch {
    return [];
  }
}

export function writeUserPresets(presets: Preset[]): void {
  try {
    window.localStorage.setItem(USER_PRESETS_KEY, JSON.stringify(presets));
  } catch {
    // Saving a preset is a convenience; failing it must not block a render.
  }
}

/**
 * Everything a preset remembers from the current settings.
 *
 * Deliberately not the subject, the script or the keywords: those are this
 * video, not the kind of video. A preset that carried them would hand the user
 * back last week's subject every time they used it.
 */
export function presetFromParams(id: string, label: string, params: VideoParams): Preset {
  return {
    id,
    label,
    hint: 'Your own preset.',
    builtIn: false,
    params: {
      video_aspect: params.video_aspect,
      video_source: params.video_source,
      voice_enabled: params.voice_enabled,
      voice_name: params.voice_name,
      voice_rate: params.voice_rate,
      voice_volume: params.voice_volume,
      subtitle_enabled: params.subtitle_enabled,
      subtitle_position: params.subtitle_position,
      font_name: params.font_name,
      font_size: params.font_size,
      text_fore_color: params.text_fore_color,
      stroke_color: params.stroke_color,
      stroke_width: params.stroke_width,
      bgm_type: params.bgm_type,
      bgm_file: params.bgm_file,
      bgm_volume: params.bgm_volume,
      video_transition_mode: params.video_transition_mode,
      video_clip_duration: params.video_clip_duration,
      paragraph_number: params.paragraph_number,
      video_duration: params.video_duration,
    },
  };
}
