/** Something that went less than well but did not stop the render.
 *
 *  The backend records the code and the numbers behind it; the sentence is
 *  written here, next to everything else the user reads. */
export interface TaskWarning {
  code: string;
  [key: string]: unknown;
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function clipShortfallMessage(warning: TaskWarning): string {
  const clips = asNumber(warning.clips) ?? 0;
  const wanted = asNumber(warning.wanted) ?? 0;
  const source = String(warning.source ?? '');
  const aspect = String(warning.aspect ?? '');

  const found =
    clips === 1
      ? 'Only 1 clip matched your search'
      : `Only ${clips} clips matched your search`;
  const room = wanted > clips ? `, and this video has room for about ${wanted}` : '';
  // What the user actually saw: the same few seconds again and again.
  const consequence = clips === 1
    ? ' — every scene is cut from that one clip.'
    : ' — each one is reused several times.';

  // Almost always the provider rather than the wording of the search. Pixabay
  // cannot filter by orientation, so a vertical search fetches fifty results
  // and keeps the two that happen to be portrait.
  const advice =
    source === 'pixabay' && aspect !== '16:9'
      ? ' Pixabay has very little vertical footage and cannot search by orientation. Pexels can, and usually returns far more.'
      : ' Try broader search terms, or a different clip source.';

  return `${found}${room}${consequence}${advice}`;
}

function bgmFailureMessage(warning: TaskWarning, service: string): string {
  const index = asNumber(warning.video_index);
  const which = index === null ? 'This video' : `Video ${index}`;
  return `${which} was rendered without background music: ${service} did not return a track.`;
}

/** Turn the warnings on a task into sentences, dropping any code this version
 *  does not know how to explain. */
export function warningMessages(warnings: unknown): string[] {
  if (!Array.isArray(warnings)) return [];
  const messages: string[] = [];
  for (const entry of warnings) {
    if (!entry || typeof entry !== 'object') continue;
    const warning = entry as TaskWarning;
    switch (warning.code) {
      case 'few_materials':
        messages.push(clipShortfallMessage(warning));
        break;
      case 'sonilo_bgm_failed':
        messages.push(bgmFailureMessage(warning, 'Sonilo'));
        break;
      case 'elevenlabs_bgm_failed':
        messages.push(bgmFailureMessage(warning, 'ElevenLabs'));
        break;
      default:
        break;
    }
  }
  return messages;
}
