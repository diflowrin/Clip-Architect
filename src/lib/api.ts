/**
 * Typed client for the Python backend.
 *
 * Every backend route wraps its payload in `{status, message, data}` and uses
 * HTTP 200 for envelope-level errors as often as not, so `request` checks both
 * the transport status and the envelope before handing back `data`.
 */

export type TaskState = -1 | 1 | 4;

export const TASK_STATE_FAILED: TaskState = -1;
export const TASK_STATE_COMPLETE: TaskState = 1;
export const TASK_STATE_PROCESSING: TaskState = 4;

export type VideoAspect = '16:9' | '9:16' | '1:1';
export type VideoConcatMode = 'random' | 'sequential';
/** What fills the frame when a clip is not the video's shape. */
export type VideoFillMode = 'blur' | 'crop' | 'bars';

export type VideoTransitionMode =
  | 'Shuffle'
  | 'FadeIn'
  | 'FadeOut'
  | 'SlideIn'
  | 'SlideOut'
  | 'ZoomIn'
  | 'ZoomOut';

/** A clip supplied by the user rather than fetched from a stock provider.
 *  `url` is the filename returned by the upload endpoint; the backend resolves
 *  it inside storage/local_videos and rejects anything outside it. */
export interface MaterialInfo {
  provider: string;
  url: string;
  duration: number;
}

export interface VideoParams {
  video_subject: string;
  video_script?: string;
  video_terms?: string | string[] | null;
  video_materials?: MaterialInfo[] | null;
  video_aspect?: VideoAspect;
  video_concat_mode?: VideoConcatMode;
  video_transition_mode?: VideoTransitionMode | null;
  /** How long one scene runs, in seconds. Zero means varied: every scene
   *  draws 5, 10 or 15 seconds. A positive value pins them all to it. */
  video_clip_duration?: number;
  video_clip_speed?: number;
  video_fill_mode?: VideoFillMode;
  match_materials_to_script?: boolean;
  video_count?: number;
  /** How long the finished video should run, in seconds.
   *
   *  With no narration it is the length exactly. With narration it is the
   *  length asked for: it becomes the word budget the script is written to,
   *  and the recorded voice decides the rest. */
  video_duration?: number;
  video_source?: string;
  video_language?: string;
  /** Whether to record a narration at all. Off means the script is written
   *  only to pick search keywords, and the video is footage plus music. */
  voice_enabled?: boolean;
  voice_name?: string;
  voice_volume?: number;
  voice_rate?: number;
  bgm_type?: string;
  bgm_file?: string;
  bgm_volume?: number;
  bgm_ducking?: boolean;
  subtitle_enabled?: boolean;
  subtitle_position?: 'top' | 'bottom' | 'center' | 'custom';
  custom_position?: number;
  font_name?: string;
  text_fore_color?: string;
  text_background_color?: boolean | string;
  rounded_subtitle_background?: boolean;
  font_size?: number;
  stroke_color?: string;
  stroke_width?: number;
  n_threads?: number;
  paragraph_number?: number;
  video_script_prompt?: string;
  custom_system_prompt?: string;
}

export interface TaskStatus {
  task_id: string;
  state: TaskState;
  progress: number;
  videos?: string[] | null;
  combined_videos?: string[] | null;
  failed_stage?: string | null;
  error?: string | null;
  /** Streamable URI per clip, aligned with `material_paths`. An entry is null
   *  when the file sits outside the task directory and cannot be previewed. */
  materials?: (string | null)[] | null;
  /** On-disk paths, in timeline order. These are what `renderReviewed` takes
   *  back — the streamable URIs above are for playback only. */
  material_paths?: string[] | null;
  script?: string | null;
  /** Playable URI for the recorded narration, once one exists. */
  audio_file?: string | null;
  /** How long the narration runs, in seconds. It is also how long the video
   *  will be: the cut is assembled to the voice track. */
  audio_duration?: number | null;
  /** What the task is doing right now, in plain words. Present only while a
   *  task is running: a percentage on its own reads as a frozen job, because
   *  single stages hold the bar in one place for minutes. */
  stage_message?: string | null;
  /** Search keywords the pipeline used. Generated when the form left them
   *  blank, so saving a project has to read them back from here. */
  terms?: string[] | null;
  [key: string]: unknown;
}

/** The speech engines the voice picker can offer. Edge is free; the other two
 *  are paid services that need credentials in Settings. */
export type VoiceEngine = 'edge' | 'azure' | 'elevenlabs';

export interface VoiceList {
  voices: string[];
  engine: VoiceEngine;
  /** Whether this engine has the credentials it needs. */
  configured: boolean;
  /** Why the list is empty, when it is. Empty otherwise. */
  message: string;
}

/** A saved project as the listing returns it: enough to identify one without
 *  shipping every setting it holds. */
export interface SavedProjectSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  /** The render this was saved from. Reopening its clips needs the task
   *  directory they live in, so this is more than a back-reference. */
  task_id: string | null;
  video_subject: string;
  /** How many clips the saved render used. Zero for a project saved before
   *  clips were stored, which reloads its settings only. */
  clip_count: number;
}

/** A saved project with the settings that reload the generate form. */
export interface SavedProject {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  task_id: string | null;
  params: VideoParams;
  /** On-disk paths of the clips the saved render used, in timeline order. */
  materials: string[];
}

/** A saved clip list checked back against the disk, ready for the review
 *  screen. `materials` holds a playback URI per kept path, or null where the
 *  clip cannot be streamed. */
export interface ReopenedClips {
  task_id: string;
  material_paths: string[];
  materials: (string | null)[];
  /** Saved clips no longer on disk. They are dropped, not an error. */
  missing: number;
  /** Whether any clip survived. Clips are the only hard requirement: a
   *  narration that is gone gets recorded again, and a silent video never had
   *  one. */
  renderable: boolean;
  /** Whether rendering will record the narration again, because this video
   *  has one and the file is no longer there. */
  needs_narration: boolean;
  script: string;
}

/** One stock clip fetched by keyword while reviewing, ready to take the place
 *  of a clip in the list. `material` is its playback URI, or null when the
 *  backend cannot stream it. */
export interface MaterialSearchResult {
  material_path: string;
  material: string | null;
  /** The keyword this clip came back for, trimmed the way the search ran. */
  search_term: string;
  /** Which stock provider answered. Not always the one the form asked for: a
   *  project made from local files still searches a stock source. */
  source: string;
}

export interface TaskListPage {
  tasks: TaskStatus[];
  total: number;
  page: number;
  page_size: number;
}

export interface FileEntry {
  name: string;
  size: number;
  file: string;
}

/** A background music track. `bundled` marks one that ships with the app, and
 *  only those take part in the "Random" draw — every bundled track has a
 *  redistribution grant recorded in LICENSES-third-party.md, and an uploaded
 *  one has none. Uploads stay pickable by name; they are simply never picked
 *  for you. */
export interface BgmFile extends FileEntry {
  bundled: boolean;
}

/** A stock clip in the shared download cache. `modified` is a Unix timestamp
 *  in seconds — the cache is unbounded, so age is how you decide what to drop. */
export interface DownloadedMaterial extends FileEntry {
  modified: number;
}

/** Extra credential field a provider needs beyond key / base URL / model.
 *  The config key is `${provider_id}_${config_suffix}`. */
export interface LlmProviderField {
  config_suffix: string;
  required: boolean;
  secret: boolean;
  default_value: string;
}

export interface LlmProvider {
  provider_id: string;
  label: string;
  api_key_url: string;
  default_model: string;
  default_base_url: string;
  show_api_key: boolean;
  show_base_url: boolean;
  requires_api_key: boolean;
  requires_model_name: boolean;
  requires_base_url: boolean;
  /** Whether this provider publishes a model list worth fetching. Providers
   *  serving a handful of models do not; an aggregator routing several
   *  hundred does, and no one can type those from memory. */
  has_model_catalog: boolean;
  extra_fields: LlmProviderField[];
}

/** One model from a provider's catalogue. `label` is the provider's own
 *  display name, falling back to the id when it publishes none. */
export interface LlmModel {
  id: string;
  label: string;
  /** Whether the provider prices this model at zero. Read from its published
   *  pricing rather than a `:free` suffix in the id, which is the less
   *  complete signal. Absent when the provider publishes no pricing at all,
   *  which counts as unknown, never as free. */
  free?: boolean;
}

/** Sections of config.toml the settings endpoint round-trips. */
export interface Settings {
  app: Record<string, unknown>;
  azure: Record<string, unknown>;
  siliconflow: Record<string, unknown>;
  minimax_tts: Record<string, unknown>;
  elevenlabs: Record<string, unknown>;
  chatterbox: Record<string, unknown>;
  ui: Record<string, unknown>;
  /** Read-only: save_config() does not persist these. */
  whisper: Record<string, unknown>;
  proxy: Record<string, unknown>;
}

interface Envelope<T> {
  status: number;
  message?: string;
  data?: T;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export class BackendApi {
  private readonly baseUrl: string;
  /**
   * Sent as `x-api-key` on every request, and in the query string on the URLs
   * the browser fetches by itself. The shell mints it per launch; see
   * `BackendInfo.api_key`.
   */
  private readonly apiKey: string;
  /**
   * Told whether the last request reached the backend at all.
   *
   * A transport failure used to land in whichever view made the call and stay
   * there: the banner is only cleared by a user action, so one failed poll
   * during a cold start left "Cannot reach the backend" on screen for the rest
   * of the session, over a backend that was answering fine. Reachability is a
   * property of the connection, not of the view that happened to notice, so
   * the connection reports it and the shell renders it once.
   */
  private readonly onReachable?: (reachable: boolean) => void;

  constructor(
    baseUrl: string,
    apiKey: string,
    onReachable?: (reachable: boolean) => void,
  ) {
    this.baseUrl = baseUrl;
    this.apiKey = apiKey;
    this.onReachable = onReachable;
  }

  /**
   * Build a URL for a route the browser will fetch on its own.
   *
   * `request` authenticates with a header, but a `<video src>`, a poster and an
   * `@font-face` cannot carry one, and those routes are protected like every
   * other. The backend therefore accepts the key from the query string on GET
   * and HEAD, and this is the only place that puts it there.
   *
   * `version` is a cache-busting token rather than a caller's job: a task keeps
   * its output URL across a re-render, so the browser has to be told the bytes
   * changed. Assembling both parameters here is what stops a caller appending
   * its own `?v=` after the key and silently turning the key into `key?v=1`.
   */
  private assetUrl(path: string, version?: number): string {
    const query = new URLSearchParams();
    if (this.apiKey) query.set('api_key', this.apiKey);
    if (version) query.set('v', String(version));
    const suffix = query.toString();
    return suffix ? `${this.baseUrl}${path}?${suffix}` : `${this.baseUrl}${path}`;
  }

  /** Absolute URL for a task artifact path returned by the backend. */
  fileUrl(path: string, version?: number): string {
    if (/^https?:\/\//i.test(path)) return path;
    return this.assetUrl(`/${path.replace(/^\/+/, '')}`, version);
  }

  /**
   * Whether a task artifact is still on disk.
   *
   * A HEAD over loopback costs a round trip and no body, and answers the one
   * question a stored path cannot: the file it names may have been deleted
   * since it was written. Only a 404 counts as gone - a backend that is
   * unreachable, or refusing for any other reason, says nothing about the file
   * and must not be read as a deletion.
   */
  async fileExists(path: string): Promise<boolean> {
    try {
      const response = await fetch(this.fileUrl(path), { method: 'HEAD' });
      return response.status !== 404;
    } catch {
      return true;
    }
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    // Upload routes send FormData, which must not get a JSON content type.
    isJsonBody = true,
  ): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: {
          ...(isJsonBody && init.body ? { 'Content-Type': 'application/json' } : {}),
          // Every route behind /api/v1 is authenticated, and so is /tasks.
          // Set before init.headers so a caller cannot drop it by accident.
          ...(this.apiKey ? { 'x-api-key': this.apiKey } : {}),
          ...init.headers,
        },
      });
    } catch {
      // fetch only rejects on transport failures, which here means the sidecar
      // died or was never reachable - worth distinguishing from an API error.
      this.onReachable?.(false);
      throw new ApiError(
        `Cannot reach the backend at ${this.baseUrl}. It may have stopped.`,
        0,
      );
    }

    // Any answer at all, including a 500, proves the connection is back.
    this.onReachable?.(true);

    let envelope: Envelope<T> | null = null;
    try {
      envelope = (await response.json()) as Envelope<T>;
    } catch {
      // Non-JSON body (e.g. an unhandled 500 rendered as HTML).
      if (!response.ok) {
        throw new ApiError(`${response.status} ${response.statusText}`, response.status);
      }
      throw new ApiError('The backend returned a malformed response.', response.status);
    }

    if (!response.ok || (envelope.status && envelope.status >= 400)) {
      throw new ApiError(
        envelope.message || `${response.status} ${response.statusText}`,
        envelope.status || response.status,
      );
    }

    return envelope.data as T;
  }

  ping(): Promise<unknown> {
    return this.request('/ping');
  }

  // --- generation ---

  createVideo(params: VideoParams): Promise<{ task_id: string }> {
    return this.request('/api/v1/videos', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  }

  /** Run the pipeline up to material selection and stop, so the clips can be
   *  reviewed before the render — which is the expensive, slow half. */
  createMaterials(params: VideoParams): Promise<{ task_id: string }> {
    return this.request('/api/v1/materials', {
      method: 'POST',
      body: JSON.stringify(params),
    });
  }

  /** Finish a reviewed task. `materials` are the `material_paths` kept after
   *  review, in the order they should appear. Script, narration and subtitles
   *  are reused from the task, so nothing is generated a second time. */
  renderReviewed(
    taskId: string,
    params: VideoParams,
    materials: string[],
  ): Promise<{ task_id: string }> {
    return this.request(`/api/v1/tasks/${encodeURIComponent(taskId)}/render`, {
      method: 'POST',
      body: JSON.stringify({ ...params, materials }),
    });
  }

  generateScript(body: {
    video_subject: string;
    video_language?: string;
    paragraph_number?: number;
    /** Seconds the script should take to read aloud. The backend turns it
     *  into the word budget it asks the model for. */
    video_duration?: number;
    /** The shape of the finished video. A vertical clip is written differently
     *  from a widescreen one, so the button has to send it too. */
    video_aspect?: string;
    video_script_prompt?: string;
    custom_system_prompt?: string;
  }): Promise<{ video_script: string }> {
    return this.request('/api/v1/scripts', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  /** Record the narration on its own, without fetching footage or rendering.
   *  Returns a task to poll; the finished record carries the audio and its
   *  duration. */
  createAudio(body: {
    video_script: string;
    video_language?: string;
    voice_name?: string;
    voice_rate?: number;
    voice_volume?: number;
  }): Promise<{ task_id: string }> {
    return this.request('/api/v1/audio', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  generateTerms(body: {
    video_subject: string;
    video_script: string;
    amount?: number;
    match_materials_to_script?: boolean;
  }): Promise<{ video_terms: string[] }> {
    return this.request('/api/v1/terms', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  // --- tasks ---

  listTasks(page = 1, pageSize = 20): Promise<TaskListPage> {
    return this.request(`/api/v1/tasks?page=${page}&page_size=${pageSize}`);
  }

  getTask(taskId: string): Promise<TaskStatus> {
    return this.request(`/api/v1/tasks/${encodeURIComponent(taskId)}`);
  }

  deleteTask(taskId: string): Promise<void> {
    return this.request(`/api/v1/tasks/${encodeURIComponent(taskId)}`, {
      method: 'DELETE',
    });
  }

  /** Check a saved clip list back against the disk and get it in the shape the
   *  review screen takes, so a project reopens on its own footage instead of
   *  matching a fresh batch against the script. */
  reopenTask(taskId: string, materials: string[]): Promise<ReopenedClips> {
    return this.request(`/api/v1/tasks/${encodeURIComponent(taskId)}/reopen`, {
      method: 'POST',
      body: JSON.stringify({ materials }),
    });
  }

  /** Fetch one stock clip for a keyword, to swap into a reviewed clip list.
   *
   *  `exclude` is what the timeline already holds: the backend skips those
   *  candidates, so asking again for the same keyword walks on to the next
   *  clip instead of handing back the one already on screen. Nothing about
   *  the task changes - the clip only reaches the video if it is rendered. */
  searchMaterial(
    taskId: string,
    body: {
      search_term: string;
      video_source?: string;
      video_aspect?: VideoAspect;
      video_clip_duration?: number;
      exclude?: string[];
    },
  ): Promise<MaterialSearchResult> {
    return this.request(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/material_search`,
      { method: 'POST', body: JSON.stringify(body) },
    );
  }

  // --- assets ---

  listBgm(): Promise<{ files: BgmFile[] }> {
    return this.request('/api/v1/musics');
  }

  listMaterials(): Promise<{ files: FileEntry[] }> {
    return this.request('/api/v1/video_materials');
  }

  /** Downloaded stock clips, newest first, with the cache's total size. */
  listDownloadedMaterials(): Promise<{
    files: DownloadedMaterial[];
    total_size: number;
  }> {
    return this.request('/api/v1/downloaded_materials');
  }

  /** Delete one cached clip from disk. Irreversible: the file is removed, not
   *  unlinked from a record, so a later render re-downloads it. */
  deleteDownloadedMaterial(name: string): Promise<void> {
    return this.request(
      `/api/v1/downloaded_materials/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    );
  }

  /** Still frame for a source clip, generated on demand and cached on disk.
   *  Used as a <video> poster so a grid of clips shows something without
   *  spinning up a decoder and a partial download per tile. */
  materialThumbnailUrl(storedName: string, version?: number): string {
    // A tasks-relative path keeps its separators; only the segments are
    // escaped, or the backend would receive one long encoded name.
    const path = storedName
      .split('/')
      .map((segment) => encodeURIComponent(segment))
      .join('/');
    return this.assetUrl(`/api/v1/material_thumbnail/${path}`, version);
  }

  /** Playback URL for a source clip, uploaded or downloaded. Both are stored
   *  under generated names, so the name is safe to put in a path. */
  materialPreviewUrl(storedName: string): string {
    return this.assetUrl(`/api/v1/material_preview/${encodeURIComponent(storedName)}`);
  }

  /** Playback URL for a background music track, bundled or uploaded. The
   *  backend resolves the name through the same helper the render uses, so a
   *  track that previews here is one the render can actually load. */
  musicPreviewUrl(fileName: string): string {
    return this.assetUrl(`/api/v1/music_preview/${encodeURIComponent(fileName)}`);
  }

  uploadMaterial(file: File): Promise<{ file: string }> {
    const form = new FormData();
    form.append('file', file);
    return this.request('/api/v1/video_materials', { method: 'POST', body: form }, false);
  }

  uploadBgm(file: File): Promise<{ file: string }> {
    const form = new FormData();
    form.append('file', file);
    return this.request('/api/v1/musics', { method: 'POST', body: form }, false);
  }

  /** Remove an uploaded track. Only uploads can be deleted — the bundled
   *  library is part of the installed package. */
  deleteBgm(fileName: string): Promise<void> {
    return this.request(`/api/v1/musics/${encodeURIComponent(fileName)}`, {
      method: 'DELETE',
    });
  }

  // --- saved projects ---

  /** Saved projects, newest first. Metadata only - `getProject` loads one. */
  listProjects(): Promise<{ projects: SavedProjectSummary[] }> {
    return this.request('/api/v1/projects');
  }

  /** Persist the current settings under a name so they can be reloaded. */
  saveProject(body: {
    title: string;
    params: VideoParams;
    task_id?: string | null;
    materials?: string[];
  }): Promise<{ project: SavedProject }> {
    return this.request('/api/v1/projects', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  getProject(id: string): Promise<{ project: SavedProject }> {
    return this.request(`/api/v1/projects/${encodeURIComponent(id)}`);
  }

  /** Rename a project, overwrite its settings, or both. */
  updateProject(
    id: string,
    patch: {
      title?: string;
      params?: VideoParams;
      task_id?: string | null;
      materials?: string[];
    },
  ): Promise<{ project: SavedProject }> {
    return this.request(`/api/v1/projects/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    });
  }

  deleteProject(id: string): Promise<void> {
    return this.request(`/api/v1/projects/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    });
  }

  // --- settings and metadata ---

  getSettings(): Promise<Settings> {
    return this.request('/api/v1/settings');
  }

  updateSettings(patch: Partial<Settings>): Promise<Settings> {
    return this.request('/api/v1/settings', {
      method: 'PUT',
      body: JSON.stringify(patch),
    });
  }

  listFonts(): Promise<{ fonts: string[] }> {
    return this.request('/api/v1/metadata/fonts');
  }

  /** The font file itself, for the subtitle preview to draw with. Only .ttf
   *  and .otf come back; a .ttc collection is 404, which is the signal to
   *  keep the fallback stack. */
  fontFileUrl(fileName: string): string {
    return this.assetUrl(`/api/v1/metadata/fonts/${encodeURIComponent(fileName)}`);
  }

  /** Voices for one speech engine. `locale` narrows the Edge catalogue; the
   *  paid engines are multilingual and ignore it. */
  listVoices(locale = '', engine: VoiceEngine = 'edge'): Promise<VoiceList> {
    const query = new URLSearchParams({ engine });
    if (locale) query.set('locale', locale);
    return this.request(`/api/v1/metadata/voices?${query.toString()}`);
  }

  listLlmProviders(): Promise<{ providers: LlmProvider[] }> {
    return this.request('/api/v1/metadata/llm-providers');
  }

  /** The models a provider publishes. A catalogue that could not be fetched
   *  comes back empty with `error` set rather than throwing: the model id is
   *  a text field underneath, and typing one is a supported way to use every
   *  provider - including for a model the catalogue has never heard of. */
  listLlmModels(
    providerId: string,
  ): Promise<{ provider_id: string; models: LlmModel[]; error: string | null }> {
    return this.request(
      `/api/v1/metadata/llm-models?provider=${encodeURIComponent(providerId)}`,
    );
  }
}
