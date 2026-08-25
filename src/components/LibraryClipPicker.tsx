import type { BackendApi, DownloadedMaterial } from '../lib/api';
import { formatAge } from '../lib/format';

interface Props {
  api: BackendApi;
  /** The slice on screen, not the whole cache. Every tile costs a thumbnail
   *  request, and a cache of hundreds would spend the first seconds of every
   *  visit fetching images nobody scrolled to. */
  clips: DownloadedMaterial[];
  /** How many the cache holds, so the footer can say what is still hidden. */
  total: number;
  /** Stored names currently picked for this render, uploads included - the
   *  render takes one list, so a clip is either in it or not, whichever
   *  screen put it there. */
  selected: string[];
  busy: boolean;
  onToggle: (file: string) => void;
  onShowMore: () => void;
}

/** Pick clips out of the download cache to use again.
 *
 *  Stock footage a render fetched stays on disk, and the same clip often fits
 *  the next script too. Reusing one costs no download, no provider quota and
 *  no wait - but only if it can be found, which means thumbnails: the files
 *  are named by content hash, so a list of names says nothing at all about
 *  what is in them. */
export function LibraryClipPicker({
  api,
  clips,
  total,
  selected,
  busy,
  onToggle,
  onShowMore,
}: Props) {
  if (total === 0) {
    return (
      <span className="field__hint">
        Nothing downloaded yet. Stock footage from a render lands here, and can
        then be used again without fetching it a second time.
      </span>
    );
  }

  return (
    <>
      <div className="clip-grid">
        {clips.map((clip) => {
          const picked = selected.includes(clip.file);
          return (
            <div
              className={`clip-tile${picked ? ' clip-tile--picked' : ''}`}
              key={clip.name}
            >
              <video
                className="clip-tile__video"
                src={api.materialPreviewUrl(clip.file)}
                poster={api.materialThumbnailUrl(clip.file)}
                controls
                // The poster is the preview; nothing of the clip itself loads
                // until someone presses play.
                preload="none"
                muted
              />
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={picked}
                  disabled={busy}
                  onChange={() => onToggle(clip.file)}
                />
                {picked ? 'In this render' : 'Use'}
              </label>
              <span className="clip-tile__age">{formatAge(clip.modified)}</span>
            </div>
          );
        })}
      </div>

      {clips.length < total && (
        <div className="library__more">
          <button
            type="button"
            className="button"
            onClick={onShowMore}
            disabled={busy}
          >
            Show more
          </button>
          <span className="field__hint">
            {clips.length} of {total}
          </span>
        </div>
      )}
    </>
  );
}
