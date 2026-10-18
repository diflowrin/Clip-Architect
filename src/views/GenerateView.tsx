import { useCallback, useEffect, useRef, useState } from 'react';

import {
  ApiError,
  BackendApi,
  TASK_STATE_COMPLETE,
  TASK_STATE_FAILED,
  TASK_STATE_PROCESSING,
  type BgmFile,
  type DownloadedMaterial,
  type FileEntry,
  type MaterialInfo,
  type SavedProject,
  type TaskStatus,
  type VideoAspect,
  type VideoParams,
  type VideoFillMode,
  type VideoTransitionMode,
  type VoiceEngine,
} from '../lib/api';
import { openPath, openUrl, revealItemInDir } from '@tauri-apps/plugin-opener';

import { formatAge, formatDuration } from '../lib/format';
import {
  clearLastRender,
  readLastRender,
  titleFor,
  writeLastRender,
  type LastRender,
} from '../lib/lastRender';
import {
  BUILT_IN_PRESETS,
  CUSTOM_LENGTH_ID,
  SCRIPT_LENGTHS,
  SUBTITLE_STYLES,
  lengthIdForParams,
  matchPreset,
  matchSubtitleStyle,
  paragraphsForSeconds,
  presetFromParams,
  readUserPresets,
  wordsForSeconds,
  writeUserPresets,
  type Preset,
} from '../lib/presets';
import { Banner } from '../components/Banner';
import { Toast, Toasts } from '../components/Toast';
import { SubtitlePreview } from '../components/SubtitlePreview';
import { LibraryClipPicker } from '../components/LibraryClipPicker';
import { MaterialReview } from '../components/MaterialReview';
import { TaskCard } from '../components/TaskCard';

/** Poll interval while a task is running. Generation takes minutes, so a
 *  sub-second poll would only add load without telling the user anything new. */
const POLL_INTERVAL_MS = 1500;

/** How much of the download cache the picker opens with. The form column is
 *  narrower than the Library page and the picker is one field among many, so
 *  a first screenful here is smaller than a first screenful there. */
const LIBRARY_PAGE_SIZE = 8;

const ASPECTS: Array<{ value: VideoAspect; label: string; hint: string }> = [
  {
    value: '9:16',
    label: 'Vertical',
    hint: '9:16 - TikTok, Reels, Shorts.',
  },
  { value: '16:9', label: 'Landscape', hint: '16:9 - YouTube.' },
  { value: '1:1', label: 'Square', hint: '1:1 - feed posts.' },
];

/** What the video is made of, as one decision instead of three.
 *
 *  Narration, music and silence are the three answers people actually give,
 *  and each is a particular combination of `voice_enabled` and `bgm_type` -
 *  two fields that sat in different sections and could be left in states
 *  nobody wants, such as a silent video still paying for a voice-over. */
type SoundMode = 'narrated' | 'music' | 'silent';

const SOUND_MODES: Array<{ value: SoundMode; label: string; hint: string }> = [
  {
    value: 'narrated',
    label: 'Narrated',
    hint: 'A voice reads the script. The narration is what sets the length.',
  },
  {
    value: 'music',
    label: 'Music only',
    hint: 'No voice. The script is still written, but only to search for footage.',
  },
  {
    value: 'silent',
    label: 'No sound',
    hint: 'No voice and no music. Clips only.',
  },
];

/** Read the mode back off the params, so the dropdown follows edits made
 *  anywhere else - loading a project, a preset, or the music picker below. */
function soundModeOf(params: VideoParams): SoundMode {
  if (params.voice_enabled !== false) return 'narrated';
  return params.bgm_type ? 'music' : 'silent';
}

/** Stock footage is mostly landscape and most of what this app makes is
 *  vertical, so this decides what the sides of the frame look like in the
 *  majority of finished videos. Blur leads because it is the only one that
 *  neither throws part of the picture away nor leaves the frame half empty. */
const FILL_MODES: Array<{ value: VideoFillMode; label: string; hint: string }> = [
  { value: 'blur', label: 'Blurred', hint: 'A blurred copy of the clip fills the sides.' },
  { value: 'crop', label: 'Fill frame', hint: 'Zoom in until the frame is full. Cuts the edges off.' },
  { value: 'bars', label: 'Black bars', hint: 'Leave the unused frame black.' },
];

// Each one overlaps the two clips it joins by about a second. The cut collects
// extra footage to make up for it, so the video still runs as long as the
// narration. The names describe what happens between two shots, which
// is what a transition is - "Fade in" used to fade up from black at every cut,
// because the effect was applied inside a clip and nothing overlapped at all.
const TRANSITIONS: Array<{ value: '' | VideoTransitionMode; label: string; hint: string }> = [
  { value: '', label: 'None', hint: 'Cut straight from one clip to the next.' },
  {
    value: 'Shuffle',
    label: 'Shuffle',
    hint: 'Pick one of the transitions below at random, once per video.',
  },
  { value: 'FadeIn', label: 'Crossfade', hint: 'Dissolve one clip into the next.' },
  { value: 'FadeOut', label: 'Dip to black', hint: 'Fade down to black and back up.' },
  { value: 'SlideIn', label: 'Slide in', hint: 'The next clip slides in over this one.' },
  { value: 'SlideOut', label: 'Slide out', hint: 'This clip slides away to uncover the next.' },
  { value: 'ZoomIn', label: 'Zoom in', hint: 'Push into the clip and hand over at the top.' },
  { value: 'ZoomOut', label: 'Zoom out', hint: 'Close the clip down to a point over the next.' },
];

const VIDEO_SOURCES = [
  { value: 'pexels', label: 'Pexels' },
  { value: 'pixabay', label: 'Pixabay' },
  { value: 'local', label: 'Local files' },
];

const SUBTITLE_POSITIONS = [
  { value: 'bottom', label: 'Bottom' },
  { value: 'center', label: 'Center' },
  { value: 'top', label: 'Top' },
  { value: 'custom', label: 'Custom' },
] as const;

/** Locales offered for the voice picker. The catalogue holds ~500 voices, so
 *  filtering by locale is what makes the dropdown usable. */
const VOICE_LOCALES = [
  { value: 'en-US', label: 'English (US)' },
  { value: 'en-GB', label: 'English (UK)' },
  { value: 'ro-RO', label: 'Romanian' },
  { value: 'de-DE', label: 'German' },
  { value: 'fr-FR', label: 'French' },
  { value: 'es-ES', label: 'Spanish' },
  { value: 'it-IT', label: 'Italian' },
  { value: '', label: 'All languages' },
];

/** Speech engines the voice picker offers. Edge costs nothing and needs no
 *  account; the other two are paid services whose credentials live in
 *  Settings, and which were unreachable from this form until now - the key
 *  could be set with no voice to spend it on. */
const VOICE_ENGINES: { value: VoiceEngine; label: string; hint: string }[] = [
  { value: 'edge', label: 'Edge (free)', hint: 'No account needed.' },
  {
    value: 'azure',
    label: 'Azure Speech',
    hint: 'Multilingual voices. Needs an Azure speech key and region.',
  },
  {
    value: 'elevenlabs',
    label: 'ElevenLabs',
    hint: 'The voices on your ElevenLabs account. Needs an API key.',
  },
];

const DEFAULT_PARAMS: VideoParams = {
  video_subject: '',
  video_script: '',
  video_terms: '',
  video_aspect: '9:16',
  video_concat_mode: 'random',
  video_transition_mode: null,
  video_fill_mode: 'blur',
  bgm_ducking: true,
  // Varied: every scene draws 5, 10 or 15 seconds. A fixed five made every
  // video a metronome and, because each scene is a separate download, made a
  // minute of video cost twelve clips.
  video_clip_duration: 0,
  video_count: 1,
  // The length asked for, in seconds. With narration it is the budget the
  // script is written to; with none it is the length of the cut. Matches the
  // "Short" chip, which is where a first-run form should start.
  video_duration: 20,
  video_source: 'pexels',
  video_language: '',
  voice_enabled: true,
  voice_name: '',
  voice_volume: 1,
  voice_rate: 1,
  bgm_type: 'random',
  bgm_file: '',
  bgm_volume: 0.2,
  subtitle_enabled: true,
  subtitle_position: 'bottom',
  custom_position: 70,
  font_name: 'NotoSans-Bold.ttf',
  text_fore_color: '#FFFFFF',
  font_size: 60,
  stroke_color: '#000000',
  stroke_width: 1.5,
  paragraph_number: 1,
  n_threads: 2,
  video_script_prompt: '',
};

/** What the shell needs to show a render from another tab: enough to say what
 *  is happening and how far along it is, and nothing that would make the
 *  sidebar a second copy of the task card. */
export interface RenderActivity {
  taskId: string;
  running: boolean;
  failed: boolean;
  progress: number;
  stage: string | null;
}

interface Props {
  api: BackendApi;
  dataDir: string | null;
  /** Whether the last request reached the backend. A message about an
   *  unreachable backend is stale the moment this turns true again. */
  reachable?: boolean;
  /** A saved project the Library asked to re-edit, or null. Applied once and
   *  then cleared by the parent, so returning to this tab does not undo any
   *  edits made since. */
  project?: SavedProject | null;
  onProjectApplied?: () => void;
  /** Reports the render in view to the shell, so a job started here stays
   *  visible from the other tabs. Null once there is no task to report. */
  onRenderActivity?: (activity: RenderActivity | null) => void;
}

/** Voice names carry their locale as the first five characters
 *  (`en-US-AriaNeural-Female`). A reloaded project pins a voice, and the
 *  locale dropdown decides which voices are fetched - leaving it on the wrong
 *  locale would drop the saved voice on the next fetch. */
function localeOf(voiceName: string | undefined): string {
  const match = /^([a-z]{2}-[A-Z]{2})/.exec(voiceName ?? '');
  if (!match) return '';
  const locale = match[1];
  // Anything the dropdown does not list is served by "All languages", which
  // fetches the full catalogue and therefore still contains the saved voice.
  return VOICE_LOCALES.some((entry) => entry.value === locale) ? locale : '';
}

/** Everything the recorded narration depends on. Changing one of these makes a
 *  preview a recording of settings no longer on screen - worse than having
 *  none, because it reports a length the render will not match. */
const VOICE_INPUTS = new Set<keyof VideoParams>([
  'video_script',
  'voice_name',
  'voice_rate',
  'voice_volume',
]);

/** ElevenLabs voices arrive as `elevenlabs:<id>:<name>`. The id identifies the
 *  voice to the API but says nothing to a person choosing one. */
function voiceLabel(voice: string): string {
  if (!voice.startsWith('elevenlabs:')) return voice;
  const parts = voice.split(':');
  return parts.length >= 3 ? parts.slice(2).join(':') : voice;
}

/** Which engine a saved voice belongs to. Reopening a project restores the
 *  voice name, and fetching the wrong engine's list would drop it: the picker
 *  falls back to the first entry whenever the saved one is not on offer. */
function engineOf(voiceName: string | undefined): VoiceEngine {
  const name = voiceName ?? '';
  if (name.startsWith('elevenlabs:')) return 'elevenlabs';
  // "en-US-AvaMultilingualNeural-V2-Female" - the suffix marks Azure's paid
  // multilingual voices, which Edge TTS does not speak.
  if (/-V2(-(Female|Male))?$/.test(name)) return 'azure';
  return 'edge';
}

export function GenerateView({
  api,
  dataDir,
  reachable = true,
  project,
  onProjectApplied,
  onRenderActivity,
}: Props) {
  const [params, setParams] = useState<VideoParams>(DEFAULT_PARAMS);
  const [voiceLocale, setVoiceLocale] = useState('en-US');
  const [voiceEngine, setVoiceEngine] = useState<VoiceEngine>('edge');
  const [voices, setVoices] = useState<string[]>([]);
  const [voiceBusy, setVoiceBusy] = useState(false);
  // The narration recorded on its own: what it sounds like, and how long the
  // video will be. Cleared whenever the script or a voice setting changes,
  // because a preview of the previous settings is worse than none.
  const [voicePreview, setVoicePreview] = useState<{
    url: string;
    duration: number;
  } | null>(null);
  // Why the list is empty, when it is: a missing key, a rejected one and an
  // unreachable service all produce the same empty dropdown otherwise.
  const [voiceMessage, setVoiceMessage] = useState('');
  const [fonts, setFonts] = useState<string[]>([]);
  const [bgmFiles, setBgmFiles] = useState<BgmFile[]>([]);
  const [materials, setMaterials] = useState<FileEntry[]>([]);
  // Stock footage earlier renders downloaded, offered for reuse next to the
  // upload control: the file is already on disk, so using it again costs no
  // download and no provider quota.
  const [libraryClips, setLibraryClips] = useState<DownloadedMaterial[]>([]);
  const [libraryShown, setLibraryShown] = useState(LIBRARY_PAGE_SIZE);
  const [selectedMaterials, setSelectedMaterials] = useState<string[]>([]);
  // The backend stores uploads under UUID filenames, so the listing has no
  // human-readable name. Remember what the user picked, for this session.
  const [materialNames, setMaterialNames] = useState<Record<string, string>>({});
  const [uploading, setUploading] = useState(false);
  const [uploadingBgm, setUploadingBgm] = useState(false);
  const [deletingBgm, setDeletingBgm] = useState(false);
  const [confirmingBgmDelete, setConfirmingBgmDelete] = useState(false);
  const bgmInput = useRef<HTMLInputElement | null>(null);

  const [scriptBusy, setScriptBusy] = useState(false);
  const [termsBusy, setTermsBusy] = useState(false);
  const [submitBusy, setSubmitBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Whether the message on screen is about the connection rather than the
  // request. A cold start can fail one poll while the backend is still opening
  // its socket, and the banner is otherwise only cleared by a user action - so
  // "Cannot reach the backend" stayed up for the rest of the session over a
  // backend that had been answering for minutes.
  const [errorIsTransport, setErrorIsTransport] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  /** Puts a failed call on screen, remembering what kind of failure it was. */
  const report = useCallback((cause: unknown) => {
    setError(describe(cause));
    // `status: 0` is the sentinel BackendApi uses for a fetch that never got
    // an answer, as opposed to one the backend refused.
    setErrorIsTransport(cause instanceof ApiError && cause.status === 0);
  }, []);

  // The connection came back, so a message about it being gone is describing
  // something that is no longer true. An API error is about the request and
  // stays until the user acts on it.
  useEffect(() => {
    if (!reachable || !errorIsTransport) return;
    setError(null);
    setErrorIsTransport(false);
  }, [reachable, errorIsTransport]);
  const [task, setTask] = useState<TaskStatus | null>(null);
  // Stock searches routinely return a clip that does not fit the script, and
  // the render that follows costs minutes. Reviewing first is the default; a
  // local source has nothing to review because the user picked the files.
  const [reviewFirst, setReviewFirst] = useState(true);
  const [awaitingReview, setAwaitingReview] = useState(false);
  // The clips the review editor is working on, held apart from `task`.
  // A running task's record is replaced whole on every progress report, so
  // between a render starting and finishing it names no materials at all - an
  // editor reading off `task` would empty itself mid-render. This snapshot is
  // what keeps the clips, and the render button, on screen after the video is
  // finished, so a change can be applied without saving the project and
  // reloading it from the Library.
  const [reviewTask, setReviewTask] = useState<TaskStatus | null>(null);
  // Bumped whenever a render is started from the editor. A re-render
  // overwrites the same output path, so without something changing in the URL
  // the player would go on showing the previous video from cache.
  const [renderRound, setRenderRound] = useState(0);
  // The progress bar lives below a form that is now several screens tall, so
  // pressing the button and being left looking at the same controls reads as
  // nothing having happened. This is what brings the work into view. The bar
  // is what gets scrolled to rather than the top of the stage: on a re-render
  // it sits under an editor taller than the window, so aiming at the stage
  // would land on the clips and leave the bar off screen again.
  const stageRef = useRef<HTMLDivElement | null>(null);
  const progressRef = useRef<HTMLDivElement | null>(null);

  // The screen opens on the last finished video rather than on an empty form.
  // Read once, synchronously, so the first paint is already the right screen
  // instead of a form that swaps under the user a frame later.
  const [lastRender, setLastRender] = useState<LastRender | null>(() => readLastRender());
  // Presets are the mechanism that takes eleven questions down to one. The
  // user's own sit beside the shipped four; both live in local storage.
  const [userPresets, setUserPresets] = useState<Preset[]>(() => readUserPresets());
  // Whether the length is being typed rather than picked.
  //
  // It cannot be read back off the numbers: pressing Custom while the form
  // says twenty seconds leaves it saying twenty seconds, which is exactly what
  // the Short chip means - so the chip stayed lit, the seconds box never
  // appeared, and Custom looked like a button that did nothing. Params the
  // chips cannot express, from a loaded project, still switch it on by
  // themselves.
  const [customLength, setCustomLength] = useState(false);
  // What the script still needs, said where the script is.
  //
  // "Enter a video subject" used to go into the banner at the top of the page,
  // which is several screens away from both buttons that raise it - press
  // Find clips at the bottom of a long form and the answer appeared somewhere
  // you were not looking. This one sits under the Script buttons, and the
  // handlers scroll it into view, so the message and the field it is about are
  // on screen together.
  const [scriptError, setScriptError] = useState<string | null>(null);
  const scriptErrorRef = useRef<HTMLDivElement | null>(null);
  const [presetName, setPresetName] = useState('');
  const [namingPreset, setNamingPreset] = useState(false);
  // `result` shows that video; `form` is the settings. A render in flight
  // always shows the form, because that is where its progress lives.
  const [mode, setMode] = useState<'result' | 'form'>(() =>
    readLastRender() ? 'result' : 'form',
  );

  // Saved project currently being edited, if any, plus the name the save panel
  // will use. `savedProjectId` is what makes "Update" possible instead of
  // piling up a new copy on every save.
  const [savedProjectId, setSavedProjectId] = useState<string | null>(null);
  const [projectTitle, setProjectTitle] = useState('');
  const [projectBusy, setProjectBusy] = useState(false);
  // The render a loaded project points at, and the clips it used. Held so
  // saving again keeps them: after a reload there is no finished task in view
  // to read them back from until the project is rendered a second time.
  const [projectTaskId, setProjectTaskId] = useState<string | null>(null);
  const [projectMaterials, setProjectMaterials] = useState<string[]>([]);

  const update = useCallback(<K extends keyof VideoParams>(key: K, value: VideoParams[K]) => {
    setParams((previous) => ({ ...previous, [key]: value }));
    if (VOICE_INPUTS.has(key)) setVoicePreview(null);
    // Typing the thing that was missing answers the message, so it goes.
    // Leaving it up would have the form still asking for what is now filled in.
    if (key === 'video_subject' || key === 'video_script') setScriptError(null);
  }, []);

  /** Say what the script is missing, and take the user to where it says so. */
  const failScript = useCallback((message: string) => {
    setScriptError(message);
    // The frame after the state change is when the banner exists to scroll to.
    requestAnimationFrame(() => {
      scriptErrorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }, []);

  // The stored record is a memory of a file, not the file itself. A video
  // deleted from the Library, or removed from disk while the app was closed,
  // leaves a record pointing at nothing - and the app opened on it, showing a
  // player that could only say the video was gone. One HEAD on mount is what
  // makes the fallback this record was always meant to have, the first-run
  // form, actually happen. The check is deliberately one-sided: only a file
  // the backend answers 404 for is dropped, so a slow or missing backend
  // never throws away a render that is still there.
  useEffect(() => {
    const entry = readLastRender();
    if (!entry) return;
    let active = true;
    void api.fileExists(entry.outputPath).then((exists) => {
      if (!active || exists) return;
      clearLastRender(entry.taskId);
      setLastRender((current) => (current?.taskId === entry.taskId ? null : current));
      // Only the screen showing that video moves. A render started between
      // mount and this answer owns the form, and must not be interrupted over
      // a video that is no longer on screen anyway.
      setMode((current) => (current === 'result' ? 'form' : current));
    });
    return () => {
      active = false;
    };
  }, [api]);

  // --- metadata ---

  useEffect(() => {
    let active = true;
    Promise.all([
      api.listFonts(),
      api.listBgm(),
      api.listMaterials(),
      api.listDownloadedMaterials(),
    ])
      .then(([fontResult, bgmResult, materialResult, libraryResult]) => {
        if (!active) return;
        setFonts(fontResult.fonts);
        setBgmFiles(bgmResult.files);
        setMaterials(materialResult.files);
        setLibraryClips(libraryResult.files);
        // A reloaded project can name local clips that have since been
        // deleted. Keeping them would show a count larger than the list and
        // fail the render at the backend, with nothing on screen saying why.
        // Both folders count as available: a clip picked out of the library is
        // as real a selection as an upload, and leaving the cache out here
        // would quietly drop every one of them on the next mount.
        const available = new Set([
          ...materialResult.files.map((entry) => entry.file),
          ...libraryResult.files.map((entry) => entry.file),
        ]);
        setSelectedMaterials((previous) =>
          previous.filter((file) => available.has(file)),
        );
      })
      .catch((cause: unknown) => {
        if (active) report(cause);
      });
    return () => {
      active = false;
    };
  }, [api, report]);

  // The cache grows with every stock render, and this form is the only place
  // that offers it back. Refetching when the source switches to local is what
  // puts a clip downloaded a minute ago in the picker without leaving the tab.
  useEffect(() => {
    if (params.video_source !== 'local') return;
    let active = true;
    api
      .listDownloadedMaterials()
      .then((result) => {
        if (active) setLibraryClips(result.files);
      })
      .catch(() => {
        // A refresh that fails leaves the list that is already on screen. The
        // mount fetch reports a backend that is genuinely unreachable; a
        // banner over a form being filled in would say it a second time.
      });
    return () => {
      active = false;
    };
  }, [api, params.video_source]);

  useEffect(() => {
    let active = true;
    api
      // The locale only narrows the Edge catalogue; sending it for the paid
      // engines would suggest a filter they do not apply.
      .listVoices(voiceEngine === 'edge' ? voiceLocale : '', voiceEngine)
      .then((result) => {
        if (!active) return;
        setVoices(result.voices);
        setVoiceMessage(result.message);
        // Keep the current pick if the new list still offers it, otherwise
        // fall back to the first voice so the form is never in an invalid state.
        setParams((previous) =>
          previous.voice_name && result.voices.includes(previous.voice_name)
            ? previous
            : { ...previous, voice_name: result.voices[0] ?? '' },
        );
      })
      .catch((cause: unknown) => {
        if (active) report(cause);
      });
    return () => {
      active = false;
    };
  }, [api, voiceLocale, voiceEngine, report]);

  // --- task polling ---

  const pollRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const startPolling = useCallback(
    (taskId: string) => {
      stopPolling();
      pollRef.current = window.setInterval(() => {
        api
          .getTask(taskId)
          .then((status) => {
            setTask(status);
            // A finished record still names the clips it was cut from, and is
            // the only place they survive - the running record dropped them.
            // Taking a copy here is what leaves the editor, and its render
            // button, in place once the video is done.
            if (status.state === TASK_STATE_COMPLETE && status.material_paths?.length) {
              setReviewTask(status);
            }
            if (status.state === TASK_STATE_COMPLETE || status.state === TASK_STATE_FAILED) {
              stopPolling();
            }
          })
          .catch((cause: unknown) => {
            // A 404 means the task was deleted elsewhere; anything else is
            // worth showing, but either way there is nothing left to poll.
            report(cause);
            stopPolling();
          });
      }, POLL_INTERVAL_MS);
    },
    [api, stopPolling, report],
  );

  // Scroll to the work when a task starts, and again when a render is started
  // from the editor. Keyed on the task id and the render round rather than on
  // every poll, so the page does not fight the user for the scroll position
  // while they are reading the clips.
  useEffect(() => {
    if (!task) return;
    // After paint, or the element being scrolled to is the one from the
    // previous state and lands in the wrong place.
    const frame = window.requestAnimationFrame(() => {
      const target = progressRef.current ?? stageRef.current;
      target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    return () => window.cancelAnimationFrame(frame);
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [task?.task_id, renderRound]);

  // Only fires when the app itself tears down: the view stays mounted across
  // tab switches, which is what keeps this poll - and the render it is
  // watching - alive while the user is somewhere else.
  useEffect(() => stopPolling, [stopPolling]);

  // Mirror the task into the shell. Read off `task` rather than reported from
  // the poll callback, so a render restored by another path - reopening a
  // saved project, say - reaches the sidebar too.
  useEffect(() => {
    if (!onRenderActivity) return;
    if (!task) {
      onRenderActivity(null);
      return;
    }
    onRenderActivity({
      taskId: task.task_id,
      running: task.state === TASK_STATE_PROCESSING,
      failed: task.state === TASK_STATE_FAILED,
      progress: task.progress ?? 0,
      stage: task.stage_message ?? null,
    });
  }, [task, onRenderActivity]);

  // Remember a finished render so the next launch opens on it. Watches `task`
  // rather than hooking the poll callback, so a render finished by any path -
  // the first pass, or a re-render from the review editor - is recorded once.
  useEffect(() => {
    if (!task || task.state !== TASK_STATE_COMPLETE) return;
    const outputs = task.videos?.length ? task.videos : (task.combined_videos ?? []);
    const output = outputs[0];
    if (!output) return;
    const entry: LastRender = {
      taskId: task.task_id,
      title: titleFor(params),
      outputPath: output,
      aspect: params.video_aspect ?? '9:16',
      durationSec: task.audio_duration ?? null,
      finishedAt: Date.now(),
      params,
    };
    writeLastRender(entry);
    setLastRender(entry);
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [task]);

  // --- saved projects ---

  /** Pull a saved project's clips back into the review screen. Failure here is
   *  never fatal: the settings are already loaded, so the fallback is the
   *  ordinary path of matching clips again on the next render. */
  const reopenSavedClips = useCallback(
    async (saved: SavedProject) => {
      if (!saved.task_id) return;
      try {
        const clips = await api.reopenTask(saved.task_id, saved.materials ?? []);
        if (!clips.renderable) {
          setNotice(
            `Loaded "${saved.title}". None of its clips are still on disk, ` +
              'so rendering will match new ones against the script.',
          );
          return;
        }
        // The review screen takes a task; this is one, assembled from what the
        // reopen endpoint verified rather than from the in-memory record,
        // which loses its clip list when the app restarts.
        const reopened: TaskStatus = {
          task_id: clips.task_id,
          state: TASK_STATE_COMPLETE,
          progress: 100,
          materials: clips.materials,
          material_paths: clips.material_paths,
          script: clips.script,
        };
        setTask(reopened);
        setReviewTask(reopened);
        setAwaitingReview(true);
        setNotice(
          `Loaded "${saved.title}" with ${clips.material_paths.length} clip` +
            `${clips.material_paths.length === 1 ? '' : 's'}` +
            (clips.missing
              ? `, ${clips.missing} no longer on disk`
              : '') +
            '. Rendering again replaces the video this project was saved from.' +
            (clips.needs_narration
              ? ' Its narration is no longer on disk and will be recorded' +
                ' again, in the voice set below.'
              : ''),
        );
      } catch (cause) {
        setNotice(
          `Loaded "${saved.title}", but its clips could not be reopened ` +
            `(${describe(cause)}). Rendering will match new ones.`,
        );
      }
    },
    [api],
  );

  // Which project this view has already applied. Without it, a re-render of
  // the parent while the prop is still set would reapply the saved settings
  // over whatever the user has typed since.
  const appliedProjectId = useRef<string | null>(null);

  useEffect(() => {
    if (!project || appliedProjectId.current === project.id) return;
    appliedProjectId.current = project.id;

    // Spread over the defaults rather than replacing them: a project saved by
    // an older build has no key for a setting added since, and the form must
    // not end up with an undefined value bound to a control.
    // oxlint-disable-next-line react/set-state-in-effect
    setParams({ ...DEFAULT_PARAMS, ...project.params });
    // A saved project carries its own seconds. Whether that is a chip or a
    // typed length is answered by the numbers themselves, not by whatever the
    // form happened to be showing before it was loaded.
    setCustomLength(false);
    setVoiceLocale(localeOf(project.params.voice_name));
    setVoiceEngine(engineOf(project.params.voice_name));
    setSelectedMaterials(
      (project.params.video_materials ?? []).map((material) => material.url),
    );
    setSavedProjectId(project.id);
    setProjectTitle(project.title);
    setProjectTaskId(project.task_id);
    setProjectMaterials(project.materials ?? []);
    setTask(null);
    setReviewTask(null);
    setRenderRound(0);
    setAwaitingReview(false);
    stopPolling();
    setError(null);
    setNotice(`Loaded "${project.title}". Adjust anything, then render again.`);
    // Re-edit lands on the editor, not on the last video. The Library button
    // says what the user asked for, and stopping at the result screen first -
    // with a "Make another video" press still to come - made the app look as
    // if it had ignored the request. The last render is still one press away
    // from here, via "Back to last video".
    // oxlint-disable-next-line react/set-state-in-effect
    setMode('form');
    onProjectApplied?.();

    // Reopening the saved footage is what makes this a re-edit rather than a
    // second attempt: the clips come back into the review screen, where they
    // can be reordered, dropped or swapped before rendering over the original.
    // Attempted for any project that names a render, not only one carrying a
    // clip list: projects saved before clips were stored still reopen, because
    // the backend falls back to the footage in the task directory.
    if (project.task_id) {
      // Same exemption as the state above: this synchronises the form with a
      // project handed in from outside React, and with what the backend finds
      // still on disk - neither is derivable during render.
      // oxlint-disable-next-line react/set-state-in-effect
      void reopenSavedClips(project);
    }
  }, [project, onProjectApplied, stopPolling, reopenSavedClips]);

  /** Put the form back to a first-run state.
   *
   *  One function rather than a handler on the Reset button, because two
   *  places need it and they were drifting: the button cleared the params and
   *  the task but left the recorded narration, the picked clips and the voice
   *  engine behind, so a "reset" form could still render the previous video's
   *  footage in the previous video's voice.
   */
  const resetForm = useCallback(() => {
    setParams(DEFAULT_PARAMS);
    setVoiceEngine('edge');
    setVoiceLocale('en-US');
    setVoicePreview(null);
    setSelectedMaterials([]);
    setTask(null);
    setReviewTask(null);
    setRenderRound(0);
    setSavedProjectId(null);
    setProjectTitle('');
    setProjectTaskId(null);
    setProjectMaterials([]);
    setAwaitingReview(false);
    setCustomLength(false);
    setError(null);
    setScriptError(null);
    setNotice(null);
    setNamingPreset(false);
    setPresetName('');
    appliedProjectId.current = null;
    stopPolling();
  }, [stopPolling]);

  // --- actions ---

  async function handleGenerateScript() {
    if (!params.video_subject.trim()) {
      failScript('Enter a video subject first - the script is written from it.');
      return;
    }
    setScriptBusy(true);
    setError(null);
    setScriptError(null);
    try {
      const result = await api.generateScript({
        video_subject: params.video_subject,
        video_language: params.video_language || '',
        paragraph_number: params.paragraph_number ?? 1,
        // Written to the same budget as a render would use, so the script the
        // button produces is the length the chips promised.
        video_duration: params.video_duration ?? 0,
        video_aspect: params.video_aspect,
        video_script_prompt: params.video_script_prompt || '',
      });
      update('video_script', result.video_script);
      setNotice('Script generated.');
    } catch (cause) {
      report(cause);
    } finally {
      setScriptBusy(false);
    }
  }

  /** Record the narration without fetching footage or rendering. It answers
   *  the two questions the form cannot: what the voice sounds like on this
   *  script, and how long the finished video will run. */
  async function handleGenerateVoice() {
    if (!params.video_script?.trim()) {
      setError('Generate or write the script first.');
      return;
    }
    if (!params.voice_name) {
      setError(voiceMessage || 'Pick a voice first.');
      return;
    }
    setVoiceBusy(true);
    setError(null);
    setVoicePreview(null);
    try {
      const started = await api.createAudio({
        video_script: params.video_script,
        video_language: params.video_language || '',
        voice_name: params.voice_name,
        voice_rate: params.voice_rate ?? 1,
        voice_volume: params.voice_volume ?? 1,
      });
      // Its own loop rather than `startPolling`: that one drives the render
      // card, and a narration run has nothing to do with the video being made.
      const finished = await pollUntilDone(started.task_id);
      if (finished.state === TASK_STATE_FAILED) {
        setError(finished.error || 'The narration could not be recorded.');
        return;
      }
      if (!finished.audio_file) {
        setError('The narration finished without producing an audio file.');
        return;
      }
      setVoicePreview({
        url: finished.audio_file,
        duration: finished.audio_duration ?? 0,
      });
    } catch (cause) {
      report(cause);
    } finally {
      setVoiceBusy(false);
    }
  }

  /** Poll one task to its end. Used by the narration run, which has no card of
   *  its own to report progress into. */
  async function pollUntilDone(taskId: string): Promise<TaskStatus> {
    for (;;) {
      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
      const status = await api.getTask(taskId);
      if (
        status.state === TASK_STATE_COMPLETE ||
        status.state === TASK_STATE_FAILED
      ) {
        return status;
      }
    }
  }

  async function handleGenerateTerms() {
    if (!params.video_script?.trim()) {
      failScript('Generate or write the script first - the keywords come out of it.');
      return;
    }
    setTermsBusy(true);
    setError(null);
    setScriptError(null);
    try {
      const result = await api.generateTerms({
        video_subject: params.video_subject,
        video_script: params.video_script,
        amount: 5,
      });
      update('video_terms', result.video_terms.join(', '));
      setNotice('Keywords generated.');
    } catch (cause) {
      report(cause);
    } finally {
      setTermsBusy(false);
    }
  }

  async function handleUploadMaterials(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      // Upload sequentially: the backend validates each file with ffprobe, and
      // firing a dozen concurrent transcodes at it only slows every one of them.
      const stored: string[] = [];
      const names: Record<string, string> = {};
      for (const file of Array.from(files)) {
        const result = await api.uploadMaterial(file);
        stored.push(result.file);
        names[result.file] = file.name;
      }
      setMaterialNames((previous) => ({ ...previous, ...names }));
      const refreshed = await api.listMaterials();
      setMaterials(refreshed.files);
      // Newly uploaded clips are selected, which is what the user just asked for.
      setSelectedMaterials((previous) => [...new Set([...previous, ...stored])]);
      setNotice(`Uploaded ${stored.length} file${stored.length === 1 ? '' : 's'}.`);
    } catch (cause) {
      report(cause);
    } finally {
      setUploading(false);
    }
  }

  async function handleUploadBgm(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;
    setUploadingBgm(true);
    setError(null);
    try {
      const result = await api.uploadBgm(file);
      const refreshed = await api.listBgm();
      setBgmFiles(refreshed.files);
      // Select what was just uploaded: choosing it by hand from a list that
      // only grew by one is busywork, and it is what the upload was for.
      setParams((previous) => ({
        ...previous,
        bgm_type: 'file',
        bgm_file: result.file,
      }));
      setNotice(`Added ${file.name} to your music.`);
    } catch (cause) {
      report(cause);
    } finally {
      setUploadingBgm(false);
    }
  }

  /** Remove one uploaded track from the music library.
   *
   *  Only uploads reach this: the bundled library is part of the installed
   *  package, and the button is not offered for it. Until this existed an
   *  upload stayed in the picker for good, which is how a voice-over recorded
   *  for something else was still there weeks later. */
  async function handleDeleteBgm(entry: BgmFile) {
    setDeletingBgm(true);
    setError(null);
    try {
      await api.deleteBgm(entry.file);
      const refreshed = await api.listBgm();
      setBgmFiles(refreshed.files);
      // The selection pointed at the file that is now gone, so it has to move.
      // Back to Random rather than to another track: it is the default, and
      // picking a replacement on the user's behalf would be a guess.
      setParams((previous) =>
        previous.bgm_file === entry.file
          ? { ...previous, bgm_type: 'random', bgm_file: '' }
          : previous,
      );
      setNotice(`Removed ${entry.name} from your music.`);
    } catch (cause) {
      report(cause);
    } finally {
      setDeletingBgm(false);
      setConfirmingBgmDelete(false);
    }
  }

  function toggleMaterial(file: string) {
    setSelectedMaterials((previous) =>
      previous.includes(file)
        ? previous.filter((entry) => entry !== file)
        : [...previous, file],
    );
  }

  const buildPayload = useCallback((): VideoParams => {
    const localMaterials: MaterialInfo[] = selectedMaterials.map((file) => ({
      provider: 'local',
      url: file,
      duration: 0,
    }));

    return {
      ...params,
      // Only meaningful for the local source; sending it otherwise would
      // override the footage the stock provider just matched.
      video_materials: params.video_source === 'local' ? localMaterials : null,
      // The backend accepts a comma separated string or a list; sending a
      // trimmed list avoids surprises with stray whitespace in the keywords.
      video_terms:
        typeof params.video_terms === 'string'
          ? params.video_terms
              .split(',')
              .map((term) => term.trim())
              .filter(Boolean)
          : params.video_terms,
    };
  }, [params, selectedMaterials]);

  async function handleSubmit() {
    if (!params.video_subject.trim()) {
      // Raised from the bottom of the form, shown next to the field it is
      // about, and scrolled to - the same answer the Script buttons give.
      failScript('Enter a video subject first - it is what the video is made from.');
      return;
    }
    // An engine whose credentials are missing offers no voices, so the picker
    // ends up empty. Stopping here beats a render that reaches the TTS stage
    // minutes later and fails on a voice that was never chosen.
    if (narrationOn && !params.voice_name) {
      setError(
        voiceMessage ||
          'Pick a voice, or turn narration off to make a video without one.',
      );
      return;
    }
    setSubmitBusy(true);
    setError(null);
    setScriptError(null);
    setNotice(null);
    setTask(null);
    setReviewTask(null);
    setRenderRound(0);
    // Own footage earns a review pass as much as stock does. The editor is
    // where clips are put in order, dropped or swapped, and an upload that
    // turned out to be the wrong take is far cheaper to catch here than after
    // a render - which was the whole reason the screen exists.
    const willReview = reviewFirst;
    setAwaitingReview(willReview);
    try {
      const payload = buildPayload();
      const result = willReview
        ? await api.createMaterials(payload)
        : await api.createVideo(payload);
      setTask({ task_id: result.task_id, state: 4, progress: 0 });
      startPolling(result.task_id);
    } catch (cause) {
      setAwaitingReview(false);
      report(cause);
    } finally {
      setSubmitBusy(false);
    }
  }

  async function handleRenderReviewed(materialPaths: string[], keepOrder: boolean) {
    // The editor outlives the record it was built from, so it is the snapshot
    // that names the task to render - `task` is only the progress it reports.
    const target = reviewTask ?? task;
    if (!target) return;
    setSubmitBusy(true);
    setError(null);
    try {
      // The reviewed list is only concatenated in the order it was sent under
      // `sequential`; `random` reshuffles it, so honouring the arrangement the
      // user just made means overriding the form's clip order here.
      const payload = buildPayload();
      await api.renderReviewed(
        target.task_id,
        keepOrder ? { ...payload, video_concat_mode: 'sequential' } : payload,
        materialPaths,
      );
      // The task id is reused, so the same card and poll loop carry on from
      // the render stage rather than starting a second, unrelated task. The
      // previous render's outputs go with it: leaving them beside a progress
      // bar would offer the old video as the one being made.
      setAwaitingReview(false);
      setRenderRound((round) => round + 1);
      // Low on purpose: a render that has to record the narration again -
      // because the voice or the script changed - starts well below the
      // assembly stage, and the first poll would otherwise walk the bar
      // backwards, which reads as a task that lost its place.
      setTask({
        task_id: target.task_id,
        state: TASK_STATE_PROCESSING,
        progress: 5,
        stage_message: 'Preparing the render',
      });
      startPolling(target.task_id);
    } catch (cause) {
      report(cause);
    } finally {
      setSubmitBusy(false);
    }
  }

  /** The settings as they should be stored: the form plus whatever the
   *  pipeline filled in. A script or keyword list the backend generated never
   *  reached the form, so saving the form alone would give back a project that
   *  writes a different script - and so renders a different video - next time. */
  function projectParams(): VideoParams {
    const payload = buildPayload();
    return {
      ...payload,
      video_script: payload.video_script?.trim() || task?.script || '',
      video_terms: task?.terms?.length ? task.terms : payload.video_terms ?? null,
    };
  }

  async function persistProject(mode: 'create' | 'update') {
    const title =
      projectTitle.trim() || params.video_subject.trim() || 'Untitled project';
    setProjectBusy(true);
    setError(null);
    // A finished render in view is the freshest record of what was used; with
    // none - a project loaded and tweaked but not re-rendered - keep what the
    // project already had, or saving would quietly drop its clips.
    const taskId = task?.task_id ?? projectTaskId;
    const materials = task?.material_paths ?? projectMaterials;
    try {
      const result =
        mode === 'update' && savedProjectId
          ? await api.updateProject(savedProjectId, {
              title,
              params: projectParams(),
              task_id: taskId,
              materials,
            })
          : await api.saveProject({
              title,
              params: projectParams(),
              task_id: taskId,
              materials,
            });
      setSavedProjectId(result.project.id);
      setProjectTitle(result.project.title);
      setProjectTaskId(result.project.task_id);
      setProjectMaterials(result.project.materials ?? []);
      setNotice(
        mode === 'update'
          ? `Updated "${result.project.title}".`
          : `Saved "${result.project.title}". Reopen it from the Library.`,
      );
    } catch (cause) {
      report(cause);
    } finally {
      setProjectBusy(false);
    }
  }

  const renderComplete =
    !!task && task.state === TASK_STATE_COMPLETE && !awaitingReview;
  // A render is on its way: the editor stays readable but nothing in it may
  // be changed, and above all it must not queue a second render of the same
  // task on top of the one already running.
  const renderRunning =
    !!task && task.state === TASK_STATE_PROCESSING && !awaitingReview;
  // The materials phase finished and nothing has been rendered from it yet.
  // Its task card would read "Complete" beside no video at all, so the editor
  // stands alone until a render gives the card something to show.
  const clipsOnly =
    !!task && task.state === TASK_STATE_COMPLETE && awaitingReview;
  // The editor's clips already have a video attached. Read off the snapshot
  // rather than the live record, which drops its outputs for the length of the
  // next render: the panel would otherwise fall back to first-pass wording,
  // and offer to discard the lot, halfway through a re-render.
  const renderedBefore = !!reviewTask?.videos?.length;
  // The panel is also offered for a project loaded but not re-rendered yet, so
  // a tweak can be saved without paying for a render first.
  const showProjectSave = renderComplete || savedProjectId !== null;

  const subtitlesOn = params.subtitle_enabled !== false;
  const narrationOn = params.voice_enabled !== false;
  const soundMode = soundModeOf(params);

  /** Apply one of the three answers to the two fields behind it. Music is
   *  restored to Random rather than to whatever was last chosen: coming back
   *  to "Music only" and getting silence would be the same defect this
   *  control exists to remove. */
  function setSoundMode(mode: SoundMode) {
    setParams((previous) => {
      if (mode === 'narrated') {
        return {
          ...previous,
          voice_enabled: true,
          bgm_type: previous.bgm_type || 'random',
        };
      }
      if (mode === 'music') {
        return {
          ...previous,
          voice_enabled: false,
          bgm_type: previous.bgm_type || 'random',
        };
      }
      return { ...previous, voice_enabled: false, bgm_type: '', bgm_file: '' };
    });
    // The recorded narration belongs to settings that no longer apply.
    if (mode !== 'narrated') setVoicePreview(null);
  }

  // The render takes one clip list, but it is filled from two places, and a
  // single count over both would leave each list unable to say how much of
  // itself is in the video.
  const uploadedNames = new Set(materials.map((entry) => entry.file));
  const libraryNames = new Set(libraryClips.map((entry) => entry.file));
  const uploadsPicked = selectedMaterials.filter((file) =>
    uploadedNames.has(file),
  ).length;
  const libraryPicked = selectedMaterials.filter((file) =>
    libraryNames.has(file),
  ).length;

  // The first pass, still searching for footage. The clips stream in
  // underneath the bar, so the bar belongs above them; once the editor is on
  // screen the bar belongs under its render button instead, which is where the
  // eye already is when a render starts.
  const findingClips =
    !!task && awaitingReview && task.state === TASK_STATE_PROCESSING;

  // Wrapped so it can be scrolled to. Only one of the two positions below is
  // ever mounted, so the single ref always points at the bar on screen.
  const progressCard =
    task && !clipsOnly ? (
      <div ref={progressRef}>
        <TaskCard task={task} api={api} dataDir={dataDir} reloadToken={renderRound} />
      </div>
    ) : null;

  // What the stage shows under the frame. Held apart from the return so the
  // clips can sit in the workspace column while the controls that shape them
  // stay in the inspector, without moving the whole block through the file.
  const stageStrip = (
    <div ref={stageRef}>
      {findingClips && progressCard}
      {findingClips && <LiveClips api={api} task={task} />}

      {(task || reviewTask) && (
        <>
          {reviewTask && (
            <MaterialReview
              // Only a different task rebuilds the editor. Re-rendering the
              // same one must not, or every edit the user made - removals,
              // order, swapped-in files - would be discarded by the very
              // render that was meant to apply them.
              key={reviewTask.task_id}
              api={api}
              task={reviewTask}
              busy={submitBusy || renderRunning}
              rendered={renderedBefore}
              matchedToScript={params.video_source !== 'local'}
              concatMode={params.video_concat_mode}
              aspect={params.video_aspect}
              source={params.video_source}
              clipDuration={params.video_clip_duration}
              renderError={error}
              onRender={(paths, keepOrder) => void handleRenderReviewed(paths, keepOrder)}
              onCancel={() => {
                setAwaitingReview(false);
                setTask(null);
                setReviewTask(null);
                stopPolling();
              }}
            />
          )}
          {!findingClips && progressCard}
        </>
      )}
    </div>
  );

  // The screen the app opens on: the video you made last, and one action.
  // The form does not exist until it is asked for, which is the whole point of
  // the structure - a first screen with one decision on it, not thirty-six.
  if (mode === 'result' && lastRender && !renderRunning && !findingClips) {
    return (
      <ResultScreen
        api={api}
        dataDir={dataDir}
        render={lastRender}
        // Clips still in the editor are work in progress. Loading the last
        // render's settings over them would silently rewrite what the pending
        // render is about, so with work open the button only goes back.
        pending={!!task || !!reviewTask}
        onStartAnother={() => {
          if (task || reviewTask) {
            setMode('form');
            return;
          }
          // A blank form, not the last video's settings. Carrying them over
          // read as the previous video refusing to leave: the subject, script
          // and keywords of a finished render are the one thing certain not to
          // describe the next one, and clearing them meant pressing Reset
          // every single time. The settings worth keeping have a home already
          // - save them as a preset, or reopen the project from the Library.
          resetForm();
          setMode('form');
        }}
      />
    );
  }

  const presets = [...BUILT_IN_PRESETS, ...userPresets];
  const activePreset = matchPreset(params, presets);
  const activeStyle = matchSubtitleStyle(params);
  // Which length chip is lit, and the seconds behind it. Custom wins when it
  // was chosen, and applies by itself to any length the three chips cannot say.
  const matchedLength = lengthIdForParams(params);
  const activeLength = customLength ? CUSTOM_LENGTH_ID : matchedLength;
  const lengthSeconds = params.video_duration ?? 30;
  // "Random" draws from the bundled tracks alone, so the two halves of the
  // library are listed apart and counted apart. Lumping them together is what
  // let a voice-over uploaded for one clip turn up as the soundtrack of an
  // unrelated re-render, with the hint cheerfully counting it as a track.
  const bundledBgm = bgmFiles.filter((entry) => entry.bundled);
  const uploadedBgm = bgmFiles.filter((entry) => !entry.bundled);
  // The track the picker is on, when it is one of the user's own. Only those
  // can be removed - the bundled library is part of the installed package.
  const selectedUpload =
    params.bgm_type === 'file'
      ? uploadedBgm.find((entry) => entry.file === params.bgm_file)
      : undefined;

  return (
    <div className="form-page">
      <div className="form-page__head">
        <div>
          <h1 className="page-title">Make a video</h1>
          <p className="page-subtitle">
            Say what it is about. Everything else has an answer already.
          </p>
        </div>
        {/* Hidden while work is in flight rather than shown doing nothing:
            the progress lives on this screen, so there is nowhere to go. */}
        {lastRender && !renderRunning && !findingClips && (
          <button type="button" className="button" onClick={() => setMode('result')}>
            Back to last video
          </button>
        )}
      </div>

      <Toasts>
        {error && <Toast tone="error" message={error} onDismiss={() => setError(null)} />}
        {notice && <Toast tone="success" message={notice} onDismiss={() => setNotice(null)} />}
      </Toasts>

      {/* The decisions that make the video, on the page. What sat behind
          "Advanced" was not advanced - a voice, a length, a script are the job
          itself. Only the pipeline facts that already have a right answer are
          still folded away, in one small block at the bottom. */}
      <section className="section">
        <div className="field">
          <span className="field__label">Kind of video</span>
          <div className="chips">
            {presets.map((preset) => (
              <button
                key={preset.id}
                type="button"
                className="chip"
                aria-pressed={activePreset === preset.id}
                onClick={() => setParams((previous) => ({ ...previous, ...preset.params }))}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <span className="field__hint">
            {presets.find((preset) => preset.id === activePreset)?.hint ??
              'Your own settings. Save them as a preset below to reuse them.'}
          </span>
        </div>

        {/* Shape, sound and source on one row. All three are single choices
            that a preset already answered, so they read as a summary of the
            preset above rather than as three questions. */}
        <div className="grid">
          <div className="field">
            <label className="field__label" htmlFor="aspect">
              Format
            </label>
            <select
              id="aspect"
              value={params.video_aspect}
              onChange={(event) => update('video_aspect', event.target.value as VideoAspect)}
            >
              {ASPECTS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <span className="field__hint">
              {ASPECTS.find((option) => option.value === params.video_aspect)?.hint}
            </span>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="sound">
              Sound
            </label>
            <select
              id="sound"
              value={soundMode}
              onChange={(event) => setSoundMode(event.target.value as SoundMode)}
            >
              {SOUND_MODES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <span className="field__hint">
              {SOUND_MODES.find((option) => option.value === soundMode)?.hint}
            </span>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="source">
              Where the footage comes from
            </label>
            <select
              id="source"
              value={params.video_source}
              onChange={(event) => update('video_source', event.target.value)}
            >
              {VIDEO_SOURCES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <span className="field__hint">
              {params.video_source === 'local'
                ? 'Your own files. Needs no account and no key.'
                : 'Stock footage. Needs a free key from that provider, set in Settings.'}
            </span>
          </div>
        </div>

        <div className="field">
          <label className="field__label" htmlFor="subject">
            What is it about?
          </label>
          <input
            id="subject"
            type="text"
            value={params.video_subject}
            placeholder="e.g. 5 habits that transform your morning"
            onChange={(event) => update('video_subject', event.target.value)}
          />
        </div>

        {/* "Paragraphs" was a fact about the prompt, not about the video.
            Nobody wants three paragraphs; they want a short video.

            The length is now a length: each chip writes both the seconds and
            the paragraph count, and the seconds become the word budget the
            script is written to. Before that, "Short" only asked for one
            paragraph - and a model that wrote a long one turned a twenty
            second video into seventy. */}
        <div className="field">
          <span className="field__label">How long</span>
          <div className="chips">
            {SCRIPT_LENGTHS.map((option) => (
              <button
                key={option.id}
                type="button"
                className="chip"
                aria-pressed={activeLength === option.id}
                onClick={() => {
                  setCustomLength(false);
                  setParams((previous) => ({
                    ...previous,
                    paragraph_number: option.paragraphs,
                    video_duration: option.seconds,
                  }));
                }}
              >
                {option.label}
              </button>
            ))}
            <button
              type="button"
              className="chip"
              aria-pressed={activeLength === CUSTOM_LENGTH_ID}
              onClick={() => {
                setCustomLength(true);
                setParams((previous) => ({
                  ...previous,
                  // Keeping the seconds already on screen is what makes this a
                  // switch to typing rather than a reset.
                  video_duration: previous.video_duration ?? 30,
                  paragraph_number: paragraphsForSeconds(previous.video_duration ?? 30),
                }));
              }}
            >
              Custom
            </button>
          </div>
          {activeLength === CUSTOM_LENGTH_ID && (
            <div className="field" style={{ margin: '10px 0 0' }}>
              <label className="field__label" htmlFor="video-duration">
                Length in seconds
              </label>
              <input
                id="video-duration"
                type="number"
                min={5}
                max={600}
                style={{ maxWidth: 160 }}
                value={params.video_duration ?? 30}
                onChange={(event) => {
                  const seconds = Number(event.target.value);
                  setParams((previous) => ({
                    ...previous,
                    video_duration: seconds,
                    // The model is told a word count and a paragraph count, and
                    // they have to agree: one paragraph per twenty-five seconds
                    // is the ratio the three named lengths use.
                    paragraph_number: paragraphsForSeconds(seconds),
                  }));
                }}
              />
            </div>
          )}
          <span className="field__hint">
            {narrationOn
              ? `About ${lengthSeconds} seconds - roughly ${wordsForSeconds(lengthSeconds)} ` +
                'words of narration. The script is written to fit; the recorded ' +
                'voice is what the cut is then timed against.'
              : `The cut runs ${lengthSeconds} seconds. With no narration to measure, ` +
                'this is also how much footage is fetched.'}
          </span>
        </div>

      </section>

      <section className="section">
        <h2 className="section__title">Script</h2>
        <p className="section__hint">
          Leave it empty and the render writes one. Script and keyword
          generation use the LLM provider configured in Settings.
        </p>

        <div className="field">
          <label className="field__label" htmlFor="script">
            Script
          </label>
          <textarea
            id="script"
            value={params.video_script ?? ''}
            placeholder="Generate automatically, or write the narration here."
            onChange={(event) => update('video_script', event.target.value)}
          />
        </div>

        <div className="field">
          <label className="field__label" htmlFor="terms">
            Stock footage keywords
          </label>
          <input
            id="terms"
            type="text"
            value={typeof params.video_terms === 'string' ? params.video_terms : ''}
            placeholder="sunrise, coffee, morning routine"
            onChange={(event) => update('video_terms', event.target.value)}
          />
          <span className="field__hint">
            Comma separated. English keywords match far more stock footage.
          </span>
        </div>

        <div className="button-row">
          <button className="button" onClick={handleGenerateScript} disabled={scriptBusy}>
            {scriptBusy ? 'Generating…' : 'Generate script'}
          </button>
          <button className="button" onClick={handleGenerateTerms} disabled={termsBusy}>
            {termsBusy ? 'Generating…' : 'Generate keywords'}
          </button>
        </div>

        {/* Under the buttons rather than at the top of the page: this is where
            the missing thing is typed, so this is where it is asked for. */}
        <div ref={scriptErrorRef}>
          {scriptError && (
            <Banner tone="error" onDismiss={() => setScriptError(null)}>
              {scriptError}
            </Banner>
          )}
        </div>
      </section>

      {/* Behind the disclosure only because the two boxes below have a right
          answer already: the language is detected, and the model needs no
          extra brief to write a usable script. */}
      <details className="advanced">
        <summary className="advanced__summary">Advanced script settings</summary>

        <section className="section">
          <div className="field">
            <label className="field__label" htmlFor="language">
              Script language
            </label>
            <input
              id="language"
              type="text"
              value={params.video_language ?? ''}
              placeholder="auto"
              onChange={(event) => update('video_language', event.target.value)}
            />
            <span className="field__hint">Leave empty to detect automatically.</span>
          </div>

          <div className="field" style={{ marginBottom: 0 }}>
            <label className="field__label" htmlFor="script-prompt">
              Extra instructions for the model
            </label>
            <textarea
              id="script-prompt"
              value={params.video_script_prompt ?? ''}
              placeholder="e.g. speak to first-time buyers, avoid jargon, end on a question"
              onChange={(event) => update('video_script_prompt', event.target.value)}
            />
            <span className="field__hint">
              Added to the script prompt as your own requirements. Used whenever
              the script is written - by the button above, or by the render when
              the Script box is empty - and ignored once that box has text,
              since then there is nothing left to write.
            </span>
          </div>
        </section>
      </details>

      {/* Only shown for local footage, because only then is there a choice to
          make: with a stock provider the clips are fetched, not picked. It sits
          on the page rather than behind a disclosure because a local render
          with nothing ticked has no footage at all. */}
      {params.video_source === 'local' && (
        <section className="section">
          <h2 className="section__title">Your footage</h2>
          <p className="section__hint">
            The clips this video is cut from. Uploads and earlier downloads both
            count towards the same list.
          </p>

          <div className="field">
            <label className="field__label" htmlFor="materials">
              Add files
            </label>
            <input
              id="materials"
              type="file"
              accept="video/*,image/*"
              multiple
              disabled={uploading}
              onChange={(event) => {
                void handleUploadMaterials(event.target.files);
                // Clear the input so re-picking the same file fires onChange.
                event.target.value = '';
              }}
            />
            <span className="field__hint">
              {uploading
                ? 'Uploading…'
                : 'Videos or images. Uploaded files stay available for later renders.'}
            </span>
          </div>

          {materials.length > 0 && (
            <div className="field">
              <span className="field__label">
                Uploaded files ({uploadsPicked} of {materials.length} in this
                render)
              </span>
              <div className="tag-list">
                {materials.map((entry) => {
                  const active = selectedMaterials.includes(entry.file);
                  return (
                    <button
                      key={entry.file}
                      type="button"
                      className="tag"
                      aria-pressed={active}
                      onClick={() => toggleMaterial(entry.file)}
                      style={{
                        cursor: 'pointer',
                        borderColor: active ? 'var(--accent)' : undefined,
                        color: active ? 'var(--accent)' : undefined,
                      }}
                    >
                      {active ? '✓ ' : ''}
                      {materialNames[entry.file] ?? entry.name}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          <div className="field">
            <span className="field__label">
              Reuse from your library ({libraryPicked} of {libraryClips.length}{' '}
              in this render)
            </span>
            <span className="field__hint" style={{ marginBottom: 8 }}>
              Stock footage earlier renders downloaded. Tick one and it goes
              into this video as it is - nothing is fetched again, and no
              provider quota is spent.
            </span>
            <LibraryClipPicker
              api={api}
              clips={libraryClips.slice(0, libraryShown)}
              total={libraryClips.length}
              selected={selectedMaterials}
              busy={submitBusy}
              onToggle={toggleMaterial}
              onShowMore={() =>
                setLibraryShown((shown) => shown + LIBRARY_PAGE_SIZE)
              }
            />
          </div>

          {selectedMaterials.length === 0 && (
            <div className="banner banner--warning" style={{ marginBottom: 0 }}>
              <div className="banner__body">
                Select at least one clip, or the render will have no footage to work with.
              </div>
            </div>
          )}
        </section>
      )}

      {/* The cut itself. Defaults that hold for almost every video, kept next
          to the footage they act on rather than in one pile at the bottom. */}
      <details className="advanced">
        <summary className="advanced__summary">Advanced clip settings</summary>

        <section className="section">
          <div className="grid">
            {/* Clip order used to live here as Random / Sequential. It was the
                same decision the review editor already owns - "render the clips
                in the order shown here" - and `handleRenderReviewed` overrode
                this value anyway. One decision, made where the clips are visible. */}

            <div className="field">
              <label className="field__label" htmlFor="fill-mode">
                Frame fill
              </label>
              <select
                id="fill-mode"
                value={params.video_fill_mode ?? 'blur'}
                onChange={(event) =>
                  update('video_fill_mode', event.target.value as VideoFillMode)
                }
              >
                {FILL_MODES.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <p className="field__hint">
                {FILL_MODES.find((o) => o.value === (params.video_fill_mode ?? 'blur'))?.hint}
              </p>
            </div>

            <div className="field">
              <label className="field__label" htmlFor="transition">
                Transition
              </label>
              <select
                id="transition"
                value={params.video_transition_mode ?? ''}
                onChange={(event) =>
                  update(
                    'video_transition_mode',
                    (event.target.value || null) as VideoTransitionMode | null,
                  )
                }
              >
                {TRANSITIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <p className="field__hint">
                {TRANSITIONS.find((o) => o.value === (params.video_transition_mode ?? ''))?.hint}
                {params.video_transition_mode
                  ? ' Each cut overlaps the two clips by about a second.'
                  : ''}
              </p>
            </div>

            <div className="field">
              <label className="field__label" htmlFor="clip-duration">
                Scene length
              </label>
              <select
                id="clip-duration"
                value={params.video_clip_duration ?? 0}
                onChange={(event) => update('video_clip_duration', Number(event.target.value))}
              >
                <option value={0}>Varied (5-15s)</option>
                <option value={5}>Every scene 5s</option>
                <option value={10}>Every scene 10s</option>
                <option value={15}>Every scene 15s</option>
              </select>
              <span className="field__hint">
                Each scene is a separate clip, so this also decides how many are
                downloaded. Varied averages ten seconds a scene - about six
                clips a minute, and a cut that does not tick.
              </span>
            </div>

            <div className="field">
              <label className="field__label" htmlFor="count">
                Videos to render
              </label>
              <input
                id="count"
                type="number"
                min={1}
                max={5}
                value={params.video_count ?? 1}
                onChange={(event) => update('video_count', Number(event.target.value))}
              />
              <span className="field__hint">
                Same script, different footage. Each one costs a full render.
              </span>
            </div>
          </div>
        </section>
      </details>

      <section className="section">
        <h2 className="section__title">Voice &amp; music</h2>
        <p className="section__hint">
          The narration is what the cut is timed against, so this is also where
          the video's length comes from.
        </p>

        {!narrationOn && (
          <p className="field__hint" style={{ marginBottom: 14 }}>
            Sound is set to
            <strong> {SOUND_MODES.find((o) => o.value === soundMode)?.label}</strong>,
            so there is no narration. The script is still written, but only to
            pick the keywords sent to the footage provider - and there are no
            subtitles, which have nothing to be timed against. Switch Sound
            back to Narrated at the top to bring the voice back.
          </p>
        )}

        <div className="grid">
          {narrationOn && (
            <>
              <div className="field">
                <label className="field__label" htmlFor="voice-engine">
                  Voice engine
                </label>
                <select
                  id="voice-engine"
                  value={voiceEngine}
                  onChange={(event) =>
                    setVoiceEngine(event.target.value as VoiceEngine)
                  }
                >
                  {VOICE_ENGINES.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <span className="field__hint">
                  {voiceMessage ||
                    VOICE_ENGINES.find((option) => option.value === voiceEngine)
                      ?.hint}
                </span>
              </div>

              {/* Only Edge has enough voices to need narrowing, and only Edge
                  splits them by locale: the paid engines are multilingual. */}
              {voiceEngine === 'edge' && (
                <div className="field">
                  <label className="field__label" htmlFor="voice-locale">
                    Voice language
                  </label>
                  <select
                    id="voice-locale"
                    value={voiceLocale}
                    onChange={(event) => setVoiceLocale(event.target.value)}
                  >
                    {VOICE_LOCALES.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              <div className="field">
                <label className="field__label" htmlFor="voice">
                  Voice ({voices.length})
                </label>
                <select
                  id="voice"
                  value={params.voice_name}
                  onChange={(event) => update('voice_name', event.target.value)}
                  disabled={!voices.length}
                >
                  {voices.map((voice) => (
                    <option key={voice} value={voice}>
                      {voiceLabel(voice)}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label className="field__label" htmlFor="voice-rate">
                  Speech rate: {(params.voice_rate ?? 1).toFixed(2)}x
                </label>
                <input
                  id="voice-rate"
                  type="range"
                  min={0.5}
                  max={2}
                  step={0.05}
                  value={params.voice_rate ?? 1}
                  onChange={(event) => update('voice_rate', Number(event.target.value))}
                />
              </div>

              <div className="field">
                <label className="field__label" htmlFor="voice-volume">
                  Voice volume: {(params.voice_volume ?? 1).toFixed(2)}
                </label>
                <input
                  id="voice-volume"
                  type="range"
                  min={0}
                  max={2}
                  step={0.05}
                  value={params.voice_volume ?? 1}
                  onChange={(event) => update('voice_volume', Number(event.target.value))}
                />
                <span className="field__hint">
                  Leave at 1.00 unless the narration sits wrong against the music.
                </span>
              </div>
            </>
          )}

          <div className="field">
            <label className="field__label" htmlFor="bgm">
              Background music
            </label>
            <select
              id="bgm"
              value={params.bgm_type === 'file' ? `file:${params.bgm_file}` : params.bgm_type}
              onChange={(event) => {
                const value = event.target.value;
                // A primed "Delete file" must not carry over to whatever is
                // selected next: the button would then name a different track
                // than the one the user armed it for.
                setConfirmingBgmDelete(false);
                if (value.startsWith('file:')) {
                  update('bgm_type', 'file');
                  update('bgm_file', value.slice('file:'.length));
                } else {
                  update('bgm_type', value);
                  update('bgm_file', '');
                }
              }}
            >
              <option value="random">Random (bundled tracks)</option>
              <option value="">None</option>
              {bundledBgm.length > 0 && (
                <optgroup label="Bundled with the app">
                  {bundledBgm.map((entry) => (
                    <option key={entry.file} value={`file:${entry.file}`}>
                      {entry.name}
                    </option>
                  ))}
                </optgroup>
              )}
              {uploadedBgm.length > 0 && (
                <optgroup label="Your uploads">
                  {uploadedBgm.map((entry) => (
                    <option key={entry.file} value={`file:${entry.file}`}>
                      {entry.name}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
            {params.bgm_type === 'file' && params.bgm_file && (
              <audio
                className="field__audio"
                // Keyed on the track so switching selections resets the
                // element instead of leaving the previous one loaded.
                key={params.bgm_file}
                src={api.musicPreviewUrl(params.bgm_file)}
                controls
                // Nothing is fetched until play: the library is browsed by
                // changing this select, and each track is several megabytes.
                preload="none"
              />
            )}
            {selectedUpload && (
              <div className="button-row" style={{ marginTop: 8 }}>
                {confirmingBgmDelete ? (
                  <>
                    <button
                      type="button"
                      className="button button--danger"
                      onClick={() => void handleDeleteBgm(selectedUpload)}
                      disabled={deletingBgm}
                    >
                      {deletingBgm ? 'Removing…' : 'Delete file'}
                    </button>
                    <button
                      type="button"
                      className="button"
                      onClick={() => setConfirmingBgmDelete(false)}
                      disabled={deletingBgm}
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="button"
                    onClick={() => setConfirmingBgmDelete(true)}
                  >
                    Remove from my music
                  </button>
                )}
                <span className="field__hint">
                  Deletes the file. Videos already rendered with it keep their
                  soundtrack.
                </span>
              </div>
            )}
            {params.bgm_type === 'random' && bundledBgm.length > 0 && (
              <span className="field__hint">
                One of the {bundledBgm.length} tracks that ship with the app is
                picked per render{uploadedBgm.length > 0 && ', and your uploads are left out'}.
                Choose a track by name to hear it first, or to use one of your own.
              </span>
            )}

            <input
              ref={bgmInput}
              type="file"
              accept=".mp3,.m4a,.aac,.wav,.flac,.ogg,.opus,.wma,audio/*"
              hidden
              onChange={(event) => {
                void handleUploadBgm(event.target.files);
                // Cleared so picking the same file twice fires onChange again.
                event.target.value = '';
              }}
            />
            <div className="button-row" style={{ marginTop: 8 }}>
              <button
                type="button"
                className="button"
                onClick={() => bgmInput.current?.click()}
                disabled={uploadingBgm}
              >
                {uploadingBgm ? 'Uploading…' : 'Upload music'}
              </button>
              <span className="field__hint">
                MP3, M4A, AAC, WAV, FLAC, OGG, OPUS or WMA, up to 30 MB. Your
                uploads are kept with the app and stay available for later
                videos. Random never picks one, so choose it by name.
              </span>
            </div>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="bgm-volume">
              Music volume: {(params.bgm_volume ?? 0.2).toFixed(2)}
            </label>
            <input
              id="bgm-volume"
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={params.bgm_volume ?? 0.2}
              onChange={(event) => update('bgm_volume', Number(event.target.value))}
            />
            {/* Sits with the volume slider because it is the same decision:
                how loud the music is allowed to be, and when. It only has an
                effect when there is a narration for the music to sit under. */}
            <label className="checkbox" style={{ marginTop: 8 }}>
              <input
                type="checkbox"
                checked={params.bgm_ducking ?? true}
                onChange={(event) => update('bgm_ducking', event.target.checked)}
              />
              <span>
                Lower the music while the narrator speaks
                <span className="field__hint">
                  {' '}
                  Drops it about 8 dB under the voice and brings it back in the
                  gaps, so the music can be set loud enough to hear.
                </span>
              </span>
            </label>
          </div>
        </div>

        {/* Recording the narration on its own answers what the form cannot:
            what this voice sounds like reading this script, and how long the
            video will be. Both are far cheaper to learn here than after a
            render that fetched footage to match. */}
        {narrationOn && (
          <div className="button-row" style={{ marginTop: 12 }}>
            <button
              type="button"
              className="button"
              onClick={handleGenerateVoice}
              disabled={voiceBusy || submitBusy}
            >
              {voiceBusy ? 'Recording…' : 'Record voice-over'}
            </button>
            {voicePreview ? (
              <span className="field__hint">
                {formatDuration(voicePreview.duration)} — the video will run
                this long, since the cut follows the voice.
              </span>
            ) : (
              <span className="field__hint">
                Optional. Hear the voice and learn the video's length before
                rendering.
              </span>
            )}
          </div>
        )}

        {narrationOn && voicePreview && (
          <audio
            className="field__audio"
            // Keyed on the file so a re-recording replaces what is loaded
            // rather than leaving the previous take in the player.
            key={voicePreview.url}
            // The API returns a relative path when no public endpoint is
            // configured, which is the desktop case: resolve it against the
            // backend the same way the render outputs are.
            src={api.fileUrl(voicePreview.url)}
            controls
            preload="none"
          />
        )}
      </section>

      <section className="section">
        <h2 className="section__title">Subtitles</h2>

        {!narrationOn && (
          <p className="field__hint" style={{ marginBottom: 10 }}>
            Subtitles need a narration to time against - the timings come out of
            the text-to-speech pass, and a silent video never makes one. The
            settings below are kept for when the voice-over goes back on.
          </p>
        )}

        {/* Never disabled. With narration off this box is a preference for
            the next render that has one, and locking it ticked said "this is
            on and you may not turn it off" - which was untrue, since a silent
            render burns nothing, and left no way to answer it either. */}
        <label className="checkbox">
          <input
            type="checkbox"
            checked={subtitlesOn}
            onChange={(event) => update('subtitle_enabled', event.target.checked)}
          />
          Burn subtitles into the video
        </label>

        {subtitlesOn && (
          <div className="field field--centred" style={{ marginTop: 14 }}>
            <div className="style-picker">
              {/* The label sits inside the left column, not above the picker:
                  that column is centred against a portrait frame twice its
                  height, so a label outside it stayed pinned to the top of the
                  row with a hand's width of blank between it and the chips. */}
              <div className="style-picker__controls">
                <span className="field__label">Style</span>
                <div className="chips">
                  {SUBTITLE_STYLES.map((style) => (
                    <button
                      key={style.id}
                      type="button"
                      className="chip"
                      aria-pressed={activeStyle === style.id}
                      onClick={() => setParams((previous) => ({ ...previous, ...style.params }))}
                    >
                      {style.label}
                    </button>
                  ))}
                </div>
                <span className="field__hint">
                  Exact: the font, its size, the colours, the outline and the
                  position. A style is a starting point - every part of it can
                  be changed below.
                </span>
              </div>
              <div className="style-picker__frame">
                <SubtitlePreview params={params} api={api} />
              </div>
            </div>
          </div>
        )}

        {subtitlesOn && (
          <div className="grid">
            <div className="field">
              <label className="field__label" htmlFor="font">
                Font
              </label>
              <select
                id="font"
                value={params.font_name}
                onChange={(event) => update('font_name', event.target.value)}
              >
                {fonts.map((font) => (
                  <option key={font} value={font}>
                    {font}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label className="field__label" htmlFor="font-size">
                Font size
              </label>
              <input
                id="font-size"
                type="number"
                min={20}
                max={120}
                value={params.font_size ?? 60}
                onChange={(event) => update('font_size', Number(event.target.value))}
              />
            </div>

            <div className="field">
              <label className="field__label" htmlFor="position">
                Position
              </label>
              <select
                id="position"
                value={params.subtitle_position}
                onChange={(event) =>
                  update(
                    'subtitle_position',
                    event.target.value as VideoParams['subtitle_position'],
                  )
                }
              >
                {SUBTITLE_POSITIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>

            {params.subtitle_position === 'custom' && (
              <div className="field">
                <label className="field__label" htmlFor="custom-position">
                  Vertical position: {params.custom_position ?? 70}%
                </label>
                <input
                  id="custom-position"
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={params.custom_position ?? 70}
                  onChange={(event) => update('custom_position', Number(event.target.value))}
                />
              </div>
            )}

            <div className="field">
              <label className="field__label" htmlFor="text-color">
                Text color
              </label>
              <div className="color-row">
                <input
                  id="text-color"
                  type="color"
                  value={params.text_fore_color ?? '#FFFFFF'}
                  onChange={(event) => update('text_fore_color', event.target.value)}
                />
                <input
                  type="text"
                  aria-label="Text color hex value"
                  value={params.text_fore_color ?? '#FFFFFF'}
                  onChange={(event) => update('text_fore_color', event.target.value)}
                />
              </div>
            </div>

            <div className="field">
              <label className="field__label" htmlFor="stroke-color">
                Outline color
              </label>
              <div className="color-row">
                <input
                  id="stroke-color"
                  type="color"
                  value={params.stroke_color ?? '#000000'}
                  onChange={(event) => update('stroke_color', event.target.value)}
                />
                <input
                  type="text"
                  aria-label="Outline color hex value"
                  value={params.stroke_color ?? '#000000'}
                  onChange={(event) => update('stroke_color', event.target.value)}
                />
              </div>
            </div>

            <div className="field">
              <label className="field__label" htmlFor="stroke-width">
                Outline width: {(params.stroke_width ?? 1.5).toFixed(1)}
              </label>
              <input
                id="stroke-width"
                type="range"
                min={0}
                max={5}
                step={0.1}
                value={params.stroke_width ?? 1.5}
                onChange={(event) => update('stroke_width', Number(event.target.value))}
              />
            </div>
          </div>
        )}
      </section>

      <div className="form-page__commit">
        {namingPreset ? (
          <div className="button-row">
            <input
              type="text"
              value={presetName}
              placeholder="Name this preset"
              onChange={(event) => setPresetName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') setNamingPreset(false);
              }}
            />
            <button
              type="button"
              className="button"
              disabled={!presetName.trim()}
              onClick={() => {
                const next = [
                  ...userPresets,
                  presetFromParams(`user-${Date.now()}`, presetName.trim(), params),
                ];
                setUserPresets(next);
                writeUserPresets(next);
                setPresetName('');
                setNamingPreset(false);
              }}
            >
              Save preset
            </button>
            <button type="button" className="button" onClick={() => setNamingPreset(false)}>
              Cancel
            </button>
          </div>
        ) : null}
        <label className="checkbox" style={{ marginBottom: 10 }}>
          <input
            type="checkbox"
            checked={reviewFirst}
            onChange={(event) => setReviewFirst(event.target.checked)}
            disabled={submitBusy}
          />
          Review clips before rendering
        </label>

        <div className="button-row">
          <button className="button button--primary" onClick={handleSubmit} disabled={submitBusy}>
            {submitBusy
              ? 'Submitting…'
              : !reviewFirst
                ? 'Generate video'
                : params.video_source === 'local'
                  ? // Nothing is searched for: the files are already here. What
                    // this run does is record the narration and check the clips.
                    'Prepare clips'
                  : 'Find clips'}
          </button>
          <button className="button" onClick={resetForm} disabled={submitBusy}>
            Reset
          </button>
          {!namingPreset && (
            <button
              type="button"
              className="button"
              onClick={() => setNamingPreset(true)}
              disabled={submitBusy}
            >
              Save as preset
            </button>
          )}
        </div>
      </div>

      {/* The clips, the progress card and the review editor, in the order they
          arrive. They live under the form because in this structure the form is
          a step towards a video, not the screen the app is about. */}
      {stageStrip}

      {/* Saving comes last because it is the last thing done: the panel only
          appears once a render is finished or a project is open, and until the
          video has been watched there is nothing to decide about keeping its
          settings. Sitting above the review editor and the progress card, it
          interrupted the one sequence the screen exists for. */}
      {showProjectSave && (
        <section className="section project-save">
          <h2 className="project-save__title">
            {savedProjectId ? 'Saved project' : 'Save this project'}
          </h2>
          <p className="project-save__hint">
            Keeps these settings - subject, script, voice, subtitles, clip
            options - so the video can be reworked later without filling the
            form in again. Saved projects live in the Library.
          </p>

          <div className="field">
            <label className="field__label" htmlFor="project-title">
              Project name
            </label>
            <input
              id="project-title"
              type="text"
              value={projectTitle}
              placeholder={params.video_subject || 'Untitled project'}
              onChange={(event) => setProjectTitle(event.target.value)}
              disabled={projectBusy}
            />
          </div>

          <div className="button-row">
            {savedProjectId ? (
              <>
                <button
                  className="button button--primary"
                  onClick={() => void persistProject('update')}
                  disabled={projectBusy}
                >
                  {projectBusy ? 'Saving…' : 'Update project'}
                </button>
                <button
                  className="button"
                  onClick={() => void persistProject('create')}
                  disabled={projectBusy}
                >
                  Save as new
                </button>
              </>
            ) : (
              <button
                className="button button--primary"
                onClick={() => void persistProject('create')}
                disabled={projectBusy}
              >
                {projectBusy ? 'Saving…' : 'Save project'}
              </button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

/** The video you made last, as the screen the app opens on.
 *
 *  Everything here is read from local storage and from the file on disk, so it
 *  survives the backend forgetting its task list on restart. The frame is
 *  aspect-neutral for the same reason it is everywhere else: 9:16 and 16:9 both
 *  sit inside it without the layout around it changing. */
function ResultScreen({
  api,
  dataDir,
  render,
  pending,
  onStartAnother,
}: {
  api: BackendApi;
  dataDir: string | null;
  render: LastRender;
  /** Whether the form still holds clips or a task that must not be discarded. */
  pending: boolean;
  onStartAnother: () => void;
}) {
  const [actionError, setActionError] = useState<string | null>(null);
  // The file can be gone - deleted from the Library, or cleaned up outside the
  // app. Rather than showing a dead player, the screen says so and offers the
  // only useful thing left, which is making another one.
  const [missing, setMissing] = useState(false);

  const ratio =
    render.aspect === '16:9' ? '16 / 9' : render.aspect === '1:1' ? '1 / 1' : '9 / 16';

  async function open() {
    setActionError(null);
    try {
      if (dataDir) await openPath(`${dataDir}/storage/tasks/${taskRelative(render.outputPath)}`);
      else await openUrl(api.fileUrl(render.outputPath));
    } catch (cause) {
      setActionError(`Could not open the video: ${String(cause)}`);
    }
  }

  async function reveal() {
    setActionError(null);
    try {
      await revealItemInDir(`${dataDir}/storage/tasks/${render.taskId}`);
    } catch (cause) {
      setActionError(`Could not open the folder: ${String(cause)}`);
    }
  }

  return (
    <div className="result">
      <div className="result__head">
        <h1 className="page-title">{render.title}</h1>
        <p className="page-subtitle">
          {/* formatAge takes a Unix timestamp in seconds, as the API sends it;
              the stored value is Date.now() in milliseconds. */}
          {formatAge(render.finishedAt / 1000)}
          {render.durationSec ? ` · ${formatDuration(render.durationSec)}` : ''}
          {` · ${render.aspect}`}
        </p>
      </div>

      {missing ? (
        <div className="result__gone">
          <p className="empty">
            This video is no longer on disk. It was deleted from the Library, or
            moved outside the app.
          </p>
        </div>
      ) : (
        <video
          className="result__video"
          style={{ aspectRatio: ratio }}
          src={api.fileUrl(render.outputPath)}
          controls
          preload="metadata"
          onError={() => setMissing(true)}
        />
      )}

      {actionError && (
        <div className="banner banner--error" style={{ margin: 0 }}>
          <div className="banner__body">{actionError}</div>
        </div>
      )}

      <div className="button-row result__actions">
        <button type="button" className="button button--primary" onClick={onStartAnother}>
          {pending ? 'Back to the form' : 'Make another'}
        </button>
        {!missing && (
          <button type="button" className="button" onClick={() => void open()}>
            Open video
          </button>
        )}
        {!missing && dataDir && (
          <button type="button" className="button" onClick={() => void reveal()}>
            Show in folder
          </button>
        )}
      </div>

      <p className="field__hint result__hint">
        {pending
          ? 'The form still has clips open. Going back leaves them exactly as they are.'
          : '"Make another" opens a blank form. To start from this video’s settings ' +
            'instead, reopen it from the Library.'}
      </p>
    </div>
  );
}

/** Task files are addressed relative to the tasks root, which is the suffix of
 *  the stream path the backend already handed back. */
function taskRelative(streamPath: string): string {
  const marker = '/tasks/';
  const index = streamPath.indexOf(marker);
  return index === -1 ? streamPath : streamPath.slice(index + marker.length);
}

/** Clips already on disk while the rest are still downloading.
 *
 *  The download stage runs for minutes and writes each clip into the task
 *  record as it lands, so there is no reason to withhold footage that is
 *  already there - the first clip is watchable long before the batch ends. The
 *  full review editor only opens once the stage finishes and the list is
 *  final; this is a read-only look at what has arrived. */
function LiveClips({ api, task }: { api: BackendApi; task: TaskStatus }) {
  const clips = (task.materials ?? []).filter(
    (uri): uri is string => typeof uri === 'string',
  );
  if (clips.length === 0) return null;

  return (
    <section className="section" style={{ marginTop: 20 }}>
      <h2 className="section__title">
        {clips.length} clip{clips.length === 1 ? '' : 's'} downloaded so far
      </h2>
      <p className="field__hint" style={{ marginBottom: 12 }}>
        Watch them now if you like. More are still coming down; you can reorder,
        remove or replace any of them once the search finishes.
      </p>
      <div className="clip-strip">
        {clips.map((uri) => (
          <video
            key={uri}
            src={api.fileUrl(uri)}
            poster={api.materialThumbnailUrl(thumbnailKeyOf(uri))}
            controls
            // Nothing loads until play: this list grows every couple of
            // seconds while the download stage is still running.
            preload="none"
          />
        ))}
      </div>
    </section>
  );
}

/** The thumbnail route addresses task files relative to the tasks root, which
 *  is the suffix of the stream URI the backend already handed back. */
function thumbnailKeyOf(streamPath: string): string {
  const marker = '/tasks/';
  const index = streamPath.indexOf(marker);
  return index === -1 ? streamPath : streamPath.slice(index + marker.length);
}

function describe(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  if (cause instanceof Error) return cause.message;
  return String(cause);
}
