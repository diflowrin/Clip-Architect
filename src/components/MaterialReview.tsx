import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type {
  BackendApi,
  TaskStatus,
  VideoAspect,
  VideoConcatMode,
} from '../lib/api';

interface Props {
  api: BackendApi;
  task: TaskStatus;
  busy: boolean;
  /** Clip order asked for on the generate form. Only `sequential` renders the
   *  material list in the order given, so it seeds the keep-order toggle. */
  concatMode: VideoConcatMode | undefined;
  /** Shape of the video being made. The render scales each clip down to fit
   *  and pads the rest with black, so previewing in this shape shows what the
   *  clip will actually look like - a hard-coded portrait frame showed a
   *  landscape project's footage as a letterboxed sliver. */
  aspect: VideoAspect | undefined;
  /** Whether this task has already produced a video. The editor stays open
   *  afterwards so the clips can be changed and rendered again, which is a
   *  different thing to say than a first pass over freshly matched footage. */
  rendered: boolean;
  /** Whether the clips were matched against the script by a stock search. The
   *  user's own uploads were not: they are used in the order given, so
   *  pairing each one with a paragraph would invent a connection that the
   *  render does not make. */
  matchedToScript: boolean;
  /** Which stock provider the form asked for. A keyword search goes to it, so
   *  a swapped-in clip comes from the same catalogue as the rest. A project
   *  made from local files has no catalogue of its own; the backend picks a
   *  stock one, because wanting a single stock clip among your own footage is
   *  an ordinary thing to ask for. */
  source: string | undefined;
  /** How long one scene runs. It sets the shortest clip worth searching for,
   *  the same way the pipeline's own download does. */
  clipDuration: number | undefined;
  /** Failure from the render request. Shown next to the button that caused it:
   *  the page-level banner sits far above and is off-screen while reviewing. */
  renderError: string | null;
  onRender: (materialPaths: string[], keepOrder: boolean) => void;
  onCancel: () => void;
}

/** One entry in the review list. `preview` is null for clips the backend
 *  cannot stream — an upload, or a clip cached outside the task directory. */
interface Clip {
  /** Stable across reordering and replacement, so removal, drag state and an
   *  in-flight upload all keep pointing at the clip they were started on. */
  id: string;
  path: string;
  preview: string | null;
  label: string;
  added: boolean;
  removed: boolean;
  /** The script paragraph this clip was matched against, captured when the
   *  list is built: once the user reorders, position no longer implies it. */
  paragraph: string | null;
  /** The keyword this clip was fetched for, when it replaced another one by
   *  search. Null for the clips the pipeline itself matched. */
  searchTerm: string | null;
  /** Which stock provider supplied it. Not always the one the form asked
   *  for: a provider with nothing new left falls through to another. */
  searchSource: string | null;
}

/** Split the script into the paragraphs the clips were matched against, so a
 *  clip can be shown next to the line it is meant to illustrate.
 *
 *  Returns nothing unless there is a paragraph for every clip. A script
 *  written as one block yields a single paragraph, and pairing it positionally
 *  would dump the whole narration onto clip 1 and leave the rest blank - which
 *  reads as a bug rather than as "these were not matched per paragraph". */
function paragraphsFor(script: string | null | undefined, clipCount: number): string[] {
  if (!script) return [];
  const paragraphs = script
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
  return paragraphs.length >= clipCount ? paragraphs : [];
}

/** Preview frame per target shape: the CSS aspect ratio, and how wide the
 *  column of thumbnails should be so a landscape frame is still legible. */
const PREVIEW_BY_ASPECT: Record<VideoAspect, { ratio: string; width: string }> = {
  '9:16': { ratio: '9 / 16', width: '168px' },
  '1:1': { ratio: '1 / 1', width: '200px' },
  '16:9': { ratio: '16 / 9', width: '280px' },
};

/** Strip the correlation id the backend puts in front of every message.
 *
 *  Every route answers `<request id>: <what went wrong>`, which is what ties
 *  a failure to a log line. Here the message is the whole point - "no new clip
 *  found for X, try another keyword" is an instruction the user acts on - and
 *  a uuid in front of it reads as a crash. Only a leading token with no space
 *  in it is removed, so a message that never had one is untouched. */
function withoutRequestId(message: string): string {
  return message.replace(/^\S+:\s+/, '');
}

function fileName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/** What the thumbnail route needs to find this clip. A downloaded clip lives
 *  under the task directory, so it is addressed the same way its stream URI
 *  is — `<task id>/<file>`; an upload is addressed by name alone. */
function thumbnailKeyOf(clip: Clip): string {
  const streamPath = clip.preview ?? '';
  const marker = '/tasks/';
  const index = streamPath.indexOf(marker);
  if (index !== -1) return streamPath.slice(index + marker.length);
  return fileName(clip.path);
}

export function MaterialReview({
  api,
  task,
  busy,
  concatMode,
  aspect,
  rendered,
  matchedToScript,
  source,
  clipDuration,
  renderError,
  onRender,
  onCancel,
}: Props) {
  const preview = PREVIEW_BY_ASPECT[aspect ?? '9:16'] ?? PREVIEW_BY_ASPECT['9:16'];
  const initial = useMemo<Clip[]>(() => {
    const paths = task.material_paths ?? [];
    const previews = task.materials ?? [];
    const paragraphs = matchedToScript
      ? paragraphsFor(task.script, paths.length)
      : [];
    return paths.map((path, index) => ({
      id: `clip-${index}`,
      path,
      preview: previews[index] ?? null,
      label: fileName(path),
      added: false,
      removed: false,
      paragraph: paragraphs[index] ?? null,
      searchTerm: null,
      searchSource: null,
    }));
  }, [task.material_paths, task.materials, task.script, matchedToScript]);

  const [clips, setClips] = useState<Clip[]>(initial);
  const [keepOrder, setKeepOrder] = useState(concatMode === 'sequential');
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadingId, setUploadingId] = useState<string | null>(null);
  // Keyword search, per card: which one has the box open, what was typed in
  // it, which one is fetching, and what the provider said when it could not.
  // All keyed by clip id rather than position, because the list is reordered
  // underneath and a search started on clip 3 must land on the same clip.
  const [searchOpenId, setSearchOpenId] = useState<string | null>(null);
  const [searchTerms, setSearchTerms] = useState<Record<string, string>>({});
  const [searchingId, setSearchingId] = useState<string | null>(null);
  const [searchErrors, setSearchErrors] = useState<Record<string, string>>({});
  // Every clip the search has offered in this session, including the ones
  // since replaced. The clip list alone is not enough to ask for something
  // new: swapping A for B takes A out of the list, so the next fetch was
  // free to hand A straight back, and the same two clips took turns forever.
  const [offeredPaths, setOfferedPaths] = useState<string[]>([]);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const replacingId = useRef<string | null>(null);
  const [expanded, setExpanded] = useState<Clip | null>(null);
  // Which card is being dragged, and which one it is hovering over. Both are
  // ids rather than positions, because the list is spliced under the drag.
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);
  // HTML5 drag needs `draggable` on the card, but a draggable card swallows
  // the pointer from the <video> controls inside it. So a card only becomes
  // draggable while the pointer is held down on its handle.
  const [armedId, setArmedId] = useState<string | null>(null);

  // A modal that traps the eye should close on Escape; without it the only way
  // out is the mouse, and the card grid behind is still the working surface.
  useEffect(() => {
    if (!expanded) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') setExpanded(null);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [expanded]);

  const keptPaths = clips.filter((clip) => !clip.removed).map((clip) => clip.path);

  /** Move a clip to another position. The new order only reaches the video if
   *  the render keeps it, so reordering turns the toggle on - otherwise the
   *  numbers move here and nothing changes in the output. */
  function moveClip(fromIndex: number, toIndex: number) {
    if (toIndex < 0 || toIndex >= clips.length || fromIndex === toIndex) return;
    setClips((previous) => {
      const next = previous.slice();
      const [moved] = next.splice(fromIndex, 1);
      next.splice(toIndex, 0, moved);
      return next;
    });
    setKeepOrder(true);
  }

  function toggleRemoved(id: string) {
    setClips((previous) =>
      previous.map((clip) =>
        clip.id === id ? { ...clip, removed: !clip.removed } : clip,
      ),
    );
  }

  function pickReplacement(id: string) {
    replacingId.current = id;
    setUploadError(null);
    fileInput.current?.click();
  }

  async function handleReplacement(files: FileList | null) {
    const file = files?.[0];
    const id = replacingId.current;
    replacingId.current = null;
    if (!file || id === null) return;

    setUploadingId(id);
    setUploadError(null);
    try {
      const result = await api.uploadMaterial(file);
      setClips((previous) =>
        previous.map((clip) =>
          clip.id === id
            ? {
                ...clip,
                path: result.file,
                // Uploads live in the local videos folder, which has its own
                // preview route - so a swapped-in clip is as playable here as
                // a downloaded one, and can be checked before rendering.
                preview: api.materialPreviewUrl(result.file),
                label: file.name,
                added: true,
                removed: false,
              }
            : clip,
        ),
      );
    } catch (cause) {
      setUploadError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setUploadingId(null);
    }
  }

  /** Open or close the keyword box on a card. It starts empty on purpose: the
   *  paragraph this clip illustrates is already printed underneath, and a
   *  stock search takes a couple of words, not a sentence of narration. */
  function toggleSearch(clip: Clip) {
    setSearchOpenId((current) => (current === clip.id ? null : clip.id));
  }

  /** Fetch a stock clip for a keyword and put it in this clip's place.
   *
   *  What the backend skips is the union of the clips on the timeline and
   *  every clip this session has already been offered - the second half is
   *  what makes "another one" mean another one. A replaced clip leaves the
   *  list, so excluding only the list let the search hand the previous clip
   *  straight back, and two clips took turns for as long as you pressed.
   *
   *  Nothing is deleted: a clip that has been swapped away stays in the
   *  download cache, where the library can offer it again. */
  async function runSearch(id: string, keyword?: string) {
    const term = (keyword ?? searchTerms[id] ?? '').trim();
    if (!term) {
      setSearchErrors((previous) => ({
        ...previous,
        [id]: 'Type a keyword to search for.',
      }));
      return;
    }

    setSearchingId(id);
    setSearchErrors((previous) => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
    try {
      const result = await api.searchMaterial(task.task_id, {
        search_term: term,
        video_source: source,
        video_aspect: aspect,
        video_clip_duration: clipDuration,
        exclude: [...new Set([...clips.map((clip) => clip.path), ...offeredPaths])],
      });
      setOfferedPaths((previous) => [...previous, result.material_path]);
      setSearchTerms((previous) => ({ ...previous, [id]: term }));
      setClips((previous) =>
        previous.map((clip) =>
          clip.id === id
            ? {
                ...clip,
                path: result.material_path,
                preview: result.material,
                label: fileName(result.material_path),
                added: false,
                removed: false,
                searchTerm: result.search_term,
                searchSource: result.source,
              }
            : clip,
        ),
      );
    } catch (cause) {
      setSearchErrors((previous) => ({
        ...previous,
        [id]: withoutRequestId(
          cause instanceof Error ? cause.message : String(cause),
        ),
      }));
    } finally {
      setSearchingId(null);
    }
  }

  function dropOnto(targetIndex: number) {
    const fromIndex = clips.findIndex((clip) => clip.id === draggingId);
    setDraggingId(null);
    setDropTargetId(null);
    setArmedId(null);
    if (fromIndex !== -1) moveClip(fromIndex, targetIndex);
  }

  return (
    <section
      className="section review"
      style={
        {
          '--review-aspect': preview.ratio,
          '--review-media-width': preview.width,
        } as CSSProperties
      }
    >
      <div className="review__header">
        <div>
          <h2 className="review__title">
            {rendered ? 'Edit clips' : 'Review clips'}
          </h2>
          <p className="review__subtitle">
            {rendered ? (
              <>
                The last render used these {clips.length} clip
                {clips.length === 1 ? '' : 's'}. Remove one, drag a clip to move
                it, fetch a different one by keyword, or swap in your own
                footage, then render again - the new video replaces the one on
                this task.
              </>
            ) : matchedToScript ? (
              <>
                {clips.length} clip{clips.length === 1 ? '' : 's'} matched your
                script. Remove anything that does not fit, drag a clip to move
                it, fetch a different one by keyword, or swap in your own
                footage. Rendering starts only when you are happy with the
                selection.
              </>
            ) : (
              <>
                {clips.length} clip{clips.length === 1 ? '' : 's'} from your own
                files, ready to render in this order. Drop anything you do not
                want, drag a clip to move it, swap in another file, or fetch a
                stock clip by keyword. Rendering starts only when you are happy
                with the selection.
              </>
            )}
          </p>
        </div>
        <span className={`badge badge--${rendered ? 'complete' : 'processing'}`}>
          {rendered ? 'Rendered' : 'Not rendered yet'}
        </span>
      </div>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={keepOrder}
          onChange={(event) => setKeepOrder(event.target.checked)}
          disabled={busy}
        />
        Render the clips in the order shown here
      </label>
      <p className="review__note">
        {keepOrder
          ? 'Each clip is used once, in this order, for up to the maximum clip duration.'
          : 'Off: the renderer shuffles the clips and may cut a long clip into several pieces.'}
      </p>

      {uploadError && (
        <p className="review__error" role="alert">
          {uploadError}
        </p>
      )}

      <input
        ref={fileInput}
        type="file"
        accept="video/*"
        hidden
        onChange={(event) => {
          void handleReplacement(event.target.files);
          event.target.value = '';
        }}
      />

      <ol className="review__list">
        {clips.map((clip, index) => {
          // Numbered by position among the kept clips, so the badge reads as
          // the place this clip takes in the video, not in the list.
          const position =
            clips.slice(0, index).filter((item) => !item.removed).length + 1;
          const classes = [
            'review__clip',
            clip.removed ? 'review__clip--removed' : '',
            draggingId === clip.id ? 'review__clip--dragging' : '',
            dropTargetId === clip.id && draggingId !== clip.id
              ? 'review__clip--drop-target'
              : '',
          ]
            .filter(Boolean)
            .join(' ');
          return (
            <li
              key={clip.id}
              className={classes}
              draggable={armedId === clip.id && !busy}
              onDragStart={(event) => {
                setDraggingId(clip.id);
                event.dataTransfer.effectAllowed = 'move';
                // A drag with no payload never starts in some engines; the id
                // is also all an outside drop target would ever see.
                event.dataTransfer.setData('text/plain', clip.id);
              }}
              onDragEnd={() => {
                setDraggingId(null);
                setDropTargetId(null);
                setArmedId(null);
              }}
              onDragOver={(event) => {
                if (!draggingId) return;
                event.preventDefault();
                event.dataTransfer.dropEffect = 'move';
                setDropTargetId(clip.id);
              }}
              onDragLeave={() =>
                setDropTargetId((current) => (current === clip.id ? null : current))
              }
              onDrop={(event) => {
                event.preventDefault();
                dropOnto(index);
              }}
            >
              <div className="review__reorder">
                <span className="review__index">
                  {clip.removed ? '—' : position}
                </span>
                <button
                  type="button"
                  className="review__handle"
                  title="Drag to reorder, or use the arrow keys"
                  aria-label={`Reorder clip ${index + 1}: the arrow keys move it`}
                  disabled={busy || clips.length < 2}
                  onMouseDown={() => setArmedId(clip.id)}
                  onMouseUp={() => setArmedId(null)}
                  onKeyDown={(event) => {
                    // Both axes: the cards wrap across rows, so neither
                    // left/right nor up/down is the whole truth about where
                    // the next slot in the timeline actually sits.
                    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
                      event.preventDefault();
                      moveClip(index, index - 1);
                    } else if (
                      event.key === 'ArrowRight' ||
                      event.key === 'ArrowDown'
                    ) {
                      event.preventDefault();
                      moveClip(index, index + 1);
                    }
                  }}
                >
                  <svg viewBox="0 0 10 16" width="10" height="16" aria-hidden="true">
                    <g fill="currentColor">
                      <circle cx="2.5" cy="3" r="1.3" />
                      <circle cx="7.5" cy="3" r="1.3" />
                      <circle cx="2.5" cy="8" r="1.3" />
                      <circle cx="7.5" cy="8" r="1.3" />
                      <circle cx="2.5" cy="13" r="1.3" />
                      <circle cx="7.5" cy="13" r="1.3" />
                    </g>
                  </svg>
                </button>
                <button
                  type="button"
                  className="review__move"
                  title="Move earlier"
                  aria-label={`Move clip ${index + 1} earlier`}
                  disabled={busy || index === 0}
                  onClick={() => moveClip(index, index - 1)}
                >
                  ←
                </button>
                <button
                  type="button"
                  className="review__move"
                  title="Move later"
                  aria-label={`Move clip ${index + 1} later`}
                  disabled={busy || index === clips.length - 1}
                  onClick={() => moveClip(index, index + 1)}
                >
                  →
                </button>
              </div>

              <div className="review__media">
                {clip.preview ? (
                  <button
                    type="button"
                    className="review__expand"
                    onClick={() => setExpanded(clip)}
                    title="Open larger"
                    aria-label={`Open clip ${index + 1} larger`}
                  >
                    <video
                      className="review__video"
                      src={api.fileUrl(clip.preview)}
                      poster={api.materialThumbnailUrl(thumbnailKeyOf(clip))}
                      controls
                      preload="none"
                      muted
                    />
                    <span className="review__expand-hint">Open larger</span>
                  </button>
                ) : (
                  <div className="review__no-preview">
                    {clip.added ? 'Your file' : 'No preview'}
                  </div>
                )}
              </div>

              <div className="review__body">
                <p className="review__clip-name" title={clip.path}>
                  {clip.label}
                </p>
                {clip.searchTerm && (
                  <p className="review__swapped">
                    Fetched for “{clip.searchTerm}”
                    {clip.searchSource ? ` · ${clip.searchSource}` : ''}
                  </p>
                )}
                {clip.paragraph && <p className="review__paragraph">{clip.paragraph}</p>}
                <div className="button-row">
                  <button
                    type="button"
                    className="button"
                    onClick={() => toggleRemoved(clip.id)}
                    disabled={busy}
                  >
                    {clip.removed ? 'Keep' : 'Remove'}
                  </button>
                  <button
                    type="button"
                    className="button"
                    onClick={() => pickReplacement(clip.id)}
                    disabled={busy || uploadingId === clip.id}
                  >
                    {uploadingId === clip.id ? 'Uploading…' : 'Replace with my file'}
                  </button>
                  <button
                    type="button"
                    className="button"
                    aria-expanded={searchOpenId === clip.id}
                    onClick={() => toggleSearch(clip)}
                    disabled={busy}
                  >
                    Find by keyword
                  </button>
                  {/* Only after a keyword has fetched something: it repeats
                      that search without reopening the box or retyping, which
                      is the whole point of trying a few clips in a row. */}
                  {clip.searchTerm && (
                    <button
                      type="button"
                      className="button"
                      title={`Fetch another clip for "${clip.searchTerm}"`}
                      onClick={() =>
                        void runSearch(clip.id, clip.searchTerm ?? undefined)
                      }
                      disabled={busy || searchingId === clip.id}
                    >
                      {searchingId === clip.id ? 'Loading…' : 'Another clip'}
                    </button>
                  )}
                </div>

                {searchOpenId === clip.id && (
                  <div className="review__search">
                    <label
                      className="review__search-label"
                      htmlFor={`search-${clip.id}`}
                    >
                      Download another clip for
                    </label>
                    <div className="review__search-row">
                      <input
                        id={`search-${clip.id}`}
                        type="text"
                        placeholder="e.g. city skyline at night"
                        value={searchTerms[clip.id] ?? ''}
                        disabled={busy || searchingId === clip.id}
                        onChange={(event) =>
                          setSearchTerms((previous) => ({
                            ...previous,
                            [clip.id]: event.target.value,
                          }))
                        }
                        onKeyDown={(event) => {
                          // The box sits inside the generate form, where Enter
                          // would otherwise submit the whole thing and start a
                          // second render.
                          if (event.key === 'Enter') {
                            event.preventDefault();
                            void runSearch(clip.id);
                          }
                        }}
                      />
                      <button
                        type="button"
                        className="button"
                        onClick={() => void runSearch(clip.id)}
                        disabled={busy || searchingId === clip.id}
                      >
                        {searchingId === clip.id ? 'Searching…' : 'Fetch clip'}
                      </button>
                    </div>
                    <p className="review__search-hint">
                      {clip.searchTerm
                        ? 'Fetch again for another clip: everything this keyword has already shown you is skipped.'
                        : 'Downloads one stock clip and puts it in this place. Nothing is rendered yet.'}
                    </p>
                  </div>
                )}

                {/* Outside the box on purpose: "Another clip" works with the
                    box closed, and its failures have to be readable there. */}
                {searchErrors[clip.id] && (
                  <p className="review__error review__error--inline" role="alert">
                    {searchErrors[clip.id]}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ol>

      {renderError && (
        <p className="review__error" role="alert">
          {renderError}
        </p>
      )}

      <div className="review__actions">
        {/* Only offered before the first render. Afterwards there is a finished
            video attached to these clips, and a button that throws both away
            sits one slip of the mouse from the one that re-renders them; the
            form's Reset still clears everything deliberately. */}
        {!rendered && (
          <button type="button" className="button" onClick={onCancel} disabled={busy}>
            Discard
          </button>
        )}
        <button
          type="button"
          className="button button--primary"
          onClick={() => onRender(keptPaths, keepOrder)}
          disabled={busy || keptPaths.length === 0}
        >
          {busy
            ? 'Rendering…'
            : `${rendered ? 'Render again with' : 'Render'} ${keptPaths.length} clip${keptPaths.length === 1 ? '' : 's'}`}
        </button>
      </div>
      {keptPaths.length === 0 && (
        <p className="review__hint">Keep at least one clip to render.</p>
      )}

      {expanded && (
        <div
          className="review__modal"
          role="dialog"
          aria-modal="true"
          aria-label={expanded.label}
          onClick={() => setExpanded(null)}
        >
          <div className="review__modal-inner" onClick={(e) => e.stopPropagation()}>
            <video
              className="review__modal-video"
              src={api.fileUrl(expanded.preview as string)}
              controls
              autoPlay
              // eslint-disable-next-line jsx-a11y/no-autofocus
              autoFocus
            />
            <div className="review__modal-bar">
              <span className="review__modal-name">{expanded.label}</span>
              <button
                type="button"
                className="button"
                onClick={() => setExpanded(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
