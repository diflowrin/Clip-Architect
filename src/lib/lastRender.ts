/**
 * The most recent finished render, remembered across app restarts.
 *
 * The backend keeps its task list in process memory (`MemoryState`; Redis is
 * off in a desktop app), so every restart forgets every render even though the
 * MP4 is still on disk. The Generate screen opens on the last video, which
 * would mean opening on nothing most of the time.
 *
 * Two facts make a frontend-only answer correct rather than a workaround: the
 * file routes are path-based (`/stream/{file_path:path}`), so a video plays
 * whether or not the backend still knows the task; and the settings that
 * produced it are the user's own input, which never needed a server.
 *
 * The limits are real and deliberate. Only the newest render is kept - this is
 * a starting point for the next video, not an archive, and the Library owns
 * history. Nothing here is authoritative: the file may be gone, in which case
 * the screen falls back to the first-run state rather than showing a dead
 * player.
 */

import type { VideoAspect, VideoParams } from './api';

const KEY = 'cliparchitect.lastRender.v1';

export interface LastRender {
  taskId: string;
  /** What to call it on screen. Derived from the subject the user typed. */
  title: string;
  /** Backend-relative path of the finished file, for `api.fileUrl`. */
  outputPath: string;
  aspect: VideoAspect;
  /** Narration length in seconds, when the record carried one. */
  durationSec: number | null;
  finishedAt: number;
  /** Everything needed to start the next video from this one. */
  params: VideoParams;
}

/**
 * Storage can throw outright, not merely return null: a webview with site data
 * blocked raises on access. Every read and write is guarded, and a failure
 * costs the convenience, never the screen.
 */
export function readLastRender(): LastRender | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const value = parsed as Partial<LastRender>;
    // Written by an older build, or edited by hand. Anything missing the two
    // fields the screen cannot render without is discarded rather than patched.
    if (!value.taskId || !value.outputPath || !value.params) return null;
    return value as LastRender;
  } catch {
    return null;
  }
}

export function writeLastRender(entry: LastRender): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(entry));
  } catch {
    // Out of quota, or storage refused. The render still happened.
  }
}

/**
 * Forget the remembered render.
 *
 * Passing the id of the record you meant to drop is what keeps a decision made
 * against an older record - a file confirmed missing after a round trip - from
 * erasing a render that finished while that check was in flight.
 */
export function clearLastRender(taskId?: string): void {
  try {
    if (taskId) {
      const current = readLastRender();
      if (!current || current.taskId !== taskId) return;
    }
    window.localStorage.removeItem(KEY);
  } catch {
    // Nothing to do; the caller is already moving on.
  }
}

/**
 * A name for a render, from what the user actually typed.
 *
 * The subject is the only human-written thing a task carries, so it is the
 * title. Falling back to the first line of the script keeps a name on renders
 * where the script was pasted and no subject was ever entered.
 */
export function titleFor(params: VideoParams): string {
  const subject = (params.video_subject || '').trim();
  if (subject) return subject;
  const firstLine = (params.video_script || '').trim().split('\n')[0]?.trim() ?? '';
  if (firstLine) return firstLine.length > 60 ? `${firstLine.slice(0, 57)}…` : firstLine;
  return 'Untitled video';
}
