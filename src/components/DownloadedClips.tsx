import { useState } from 'react';
import type { BackendApi, DownloadedMaterial } from '../lib/api';
import { formatAge, formatSize } from '../lib/format';

interface Props {
  api: BackendApi;
  /** The slice currently on screen, not the whole cache. */
  clips: DownloadedMaterial[];
  /** How many clips exist in total, so the footer can say what is hidden. */
  shownOf: number;
  totalSize: number;
  busy: boolean;
  onDelete: (name: string) => Promise<void>;
}

export function DownloadedClips({
  api,
  clips,
  shownOf,
  totalSize,
  busy,
  onDelete,
}: Props) {
  const [confirming, setConfirming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  async function remove(name: string) {
    setDeleting(name);
    try {
      await onDelete(name);
    } finally {
      setDeleting(null);
      setConfirming(null);
    }
  }

  if (clips.length === 0) {
    return (
      <div className="empty">
        No downloaded clips cached. Stock footage lands here as you generate.
      </div>
    );
  }

  return (
    <div className="clip-grid">
      {clips.map((clip) => {
        const isConfirming = confirming === clip.name;
        return (
          <div className="clip-tile" key={clip.name}>
            <video
              className="clip-tile__video"
              src={api.materialPreviewUrl(clip.file)}
              poster={api.materialThumbnailUrl(clip.file)}
              controls
              // The poster carries the preview, so nothing of the video itself
              // is fetched until someone presses play. With dozens of tiles
              // that is the difference between a page and a download queue.
              preload="none"
              muted
            />
            <div className="clip-tile__meta">
              <span className="clip-tile__size">{formatSize(clip.size)}</span>
              <span className="clip-tile__age">{formatAge(clip.modified)}</span>
            </div>
            {isConfirming ? (
              <div className="button-row">
                <button
                  type="button"
                  className="button button--danger"
                  onClick={() => void remove(clip.name)}
                  disabled={deleting === clip.name}
                >
                  {deleting === clip.name ? 'Deleting…' : 'Delete file'}
                </button>
                <button
                  type="button"
                  className="button"
                  onClick={() => setConfirming(null)}
                  disabled={deleting === clip.name}
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="button"
                onClick={() => setConfirming(clip.name)}
                disabled={busy}
              >
                Delete
              </button>
            )}
          </div>
        );
      })}
      <p className="clip-grid__total">
        {shownOf} clip{shownOf === 1 ? '' : 's'}, {formatSize(totalSize)} on disk.
        Deleting removes the file; a later render downloads it again.
      </p>
    </div>
  );
}
