import { useState } from 'react';
import { openPath, openUrl, revealItemInDir } from '@tauri-apps/plugin-opener';

import {
  BackendApi,
  TASK_STATE_COMPLETE,
  TASK_STATE_FAILED,
  type TaskStatus,
} from '../lib/api';
import { formatDuration } from '../lib/format';
import { warningMessages } from '../lib/warnings';

interface Props {
  task: TaskStatus;
  api: BackendApi;
  /** App data directory, used to locate renders on disk. Without it the
   *  "Show in folder" action is hidden rather than pointing somewhere wrong. */
  dataDir?: string | null;
  /** Changes whenever this task is rendered again. Every render of a task
   *  writes the same `final-1.mp4`, so the URL alone is not enough to tell the
   *  browser the video is a different one - the player and its poster would
   *  both come back from cache and show the previous render. */
  reloadToken?: number;
  onDelete?: (taskId: string) => void;
}

/** The thumbnail route addresses task files relative to the tasks root, which
 *  is exactly the suffix of the stream URI the backend already handed back. */
function thumbnailKeyOf(streamPath: string): string {
  const marker = '/tasks/';
  const index = streamPath.indexOf(marker);
  return index === -1 ? streamPath : streamPath.slice(index + marker.length);
}

function statusLabel(task: TaskStatus): { text: string; modifier: string } {
  if (task.state === TASK_STATE_COMPLETE) return { text: 'Complete', modifier: 'complete' };
  if (task.state === TASK_STATE_FAILED) return { text: 'Failed', modifier: 'failed' };
  return { text: 'Rendering', modifier: 'processing' };
}

export function TaskCard({ task, api, dataDir, reloadToken, onDelete }: Props) {
  const [actionError, setActionError] = useState<string | null>(null);
  // Deleting a render destroys the video file, and nothing regenerates it
  // short of paying for the render again. The cheap, reversible action in this
  // app already asks twice; the expensive, irreversible one has to.
  const [confirming, setConfirming] = useState(false);
  // Only after a re-render, so a task opened once keeps the plain URL the
  // browser may already have cached. The URLs are assembled by the api client,
  // which also has to fit the API key into the same query string.
  const cacheBuster = reloadToken ? `v=${reloadToken}` : '';

  const status = statusLabel(task);
  // A render that finished with less footage than it wanted, or without the
  // music it was asked for, is still a finished render - so it is said here,
  // next to the video, rather than being left for the user to notice.
  const warnings = warningMessages(task.warnings);
  // `videos` holds the finished renders - subtitles burned in, narration and
  // music mixed. `combined_videos` is the silent intermediate the subtitle
  // pass consumes. Preferring the latter, as this once did, showed and opened
  // a video with no sound and no subtitles.
  const outputs = task.videos?.length ? task.videos : task.combined_videos ?? [];
  const isRunning = task.state !== TASK_STATE_COMPLETE && task.state !== TASK_STATE_FAILED;

  async function handleOpen(path: string) {
    setActionError(null);
    try {
      // Anchor navigation is inert inside the webview, so the file goes to the
      // OS. Prefer the path on disk: that opens the user's video player rather
      // than a browser tab, and it keeps working after the backend stops. The
      // loopback URL stays as the fallback for the case where the shell did
      // not report a data directory.
      if (dataDir) {
        await openPath(`${dataDir}/storage/tasks/${thumbnailKeyOf(path)}`);
      } else {
        await openUrl(api.fileUrl(path));
      }
    } catch (cause) {
      setActionError(`Could not open the video: ${String(cause)}`);
    }
  }

  async function handleReveal() {
    setActionError(null);
    try {
      // Task artifacts live under <data dir>/storage/tasks/<task id>/.
      await revealItemInDir(`${dataDir}/storage/tasks/${task.task_id}`);
    } catch (cause) {
      setActionError(`Could not open the folder: ${String(cause)}`);
    }
  }

  return (
    <article className="task">
      <header className="task__header">
        <span className="task__id">{task.task_id}</span>
        {/* The cut is assembled to the narration, so its length is the
            video's. Worth stating next to the result: it is the one property
            of the output a poster frame cannot show. */}
        {task.audio_duration ? (
          <span className="task__id">{formatDuration(task.audio_duration)}</span>
        ) : null}
        <span className={`badge badge--${status.modifier}`}>{status.text}</span>
      </header>

      {isRunning && (
        <>
          <div className="progress">
            <div className="progress__bar" style={{ width: `${task.progress ?? 0}%` }} />
          </div>
          <span className="field__hint">
            {task.progress ?? 0}%
            {task.stage_message
              ? ` — ${task.stage_message}`
              : ' — this can take several minutes.'}
          </span>
        </>
      )}

      {task.state === TASK_STATE_FAILED && (
        <div className="banner banner--error" style={{ margin: 0 }}>
          <div className="banner__body">
            {task.failed_stage && <strong>{task.failed_stage}: </strong>}
            {task.error || 'The task failed without reporting a reason.'}
          </div>
        </div>
      )}

      {warnings.map((message) => (
        <div key={message} className="banner banner--warning" style={{ margin: 0 }}>
          <div className="banner__body">{message}</div>
        </div>
      ))}

      {actionError && (
        <div className="banner banner--error" style={{ margin: 0 }}>
          <div className="banner__body">{actionError}</div>
        </div>
      )}

      {outputs.length > 0 && (
        <div className={`video-grid${outputs.length === 1 ? ' video-grid--single' : ''}`}>
          {outputs.map((path) => (
            <video
              // The token is part of the key as well as the URL: React reuses
              // an element whose key did not change, and a reused player does
              // not reload just because its src attribute was rewritten.
              key={`${path}${cacheBuster}`}
              src={api.fileUrl(path, reloadToken)}
              poster={api.materialThumbnailUrl(thumbnailKeyOf(path), reloadToken)}
              controls
              // The poster is the preview; nothing of the video loads until
              // play is pressed.
              preload="none"
            />
          ))}
        </div>
      )}

      {(outputs.length > 0 || onDelete) && (
        <div className="button-row">
          {outputs.map((path, index) => (
            <button key={path} type="button" className="button" onClick={() => handleOpen(path)}>
              {outputs.length > 1 ? `Open video ${index + 1}` : 'Open video'}
            </button>
          ))}
          {outputs.length > 0 && dataDir && (
            <button type="button" className="button" onClick={handleReveal}>
              Show in folder
            </button>
          )}
          {onDelete &&
            (confirming ? (
              // Two steps, as in the download cache - but the stakes are not
              // the same. A cached clip re-downloads for free; this is minutes
              // of render time and provider quota, and the file goes with it.
              // So the confirming button names what is about to be destroyed.
              <>
                <button
                  type="button"
                  className="button button--danger task__delete"
                  onClick={() => {
                    setConfirming(false);
                    onDelete(task.task_id);
                  }}
                >
                  Delete render and video
                </button>
                <button type="button" className="button" onClick={() => setConfirming(false)}>
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="button button--danger task__delete"
                onClick={() => setConfirming(true)}
                disabled={isRunning}
                title={isRunning ? 'Cannot delete a task while it is running' : undefined}
              >
                Delete
              </button>
            ))}
        </div>
      )}
    </article>
  );
}
