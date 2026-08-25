/** Human-readable shapes for the numbers the API returns about files.
 *
 *  Shared by the two screens that list the download cache - the Library, which
 *  is about clearing it out, and the picker in the Generate form, which is
 *  about reusing what is in it. Two copies of "how old is this" would drift
 *  the moment one of them gained a unit the other did not. */

export function formatSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  return `${Math.round(bytes / 1024 ** 2)} MB`;
}

/** `modifiedSeconds` is a Unix timestamp in seconds, as the API sends it. */
export function formatAge(modifiedSeconds: number): string {
  const minutes = Math.round((Date.now() - modifiedSeconds * 1000) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return `${days} d ago`;
}

/** Seconds as `m:ss`, the way a video length is written. Used for the
 *  narration and for the finished cut, which is as long as its voice track. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, '0')}`;
}
