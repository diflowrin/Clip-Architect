/**
 * Creates the directories that tauri.conf.json lists under bundle.resources.
 *
 * Tauri's build script aborts with "resource path ... doesn't exist" if any of
 * them is missing, and two of them are legitimately absent much of the time:
 * backend/dist/backend only appears after PyInstaller runs, and PyInstaller
 * deletes and recreates it on every rebuild. Hitting that error tells you
 * nothing about the actual cause, so create the directories up front and let
 * the runtime report anything genuinely missing with a useful message.
 */

import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectDir = dirname(dirname(fileURLToPath(import.meta.url)));

/** Directories Tauri must find, with a note explaining an empty one. */
const required = [
  {
    path: join(projectDir, 'backend', 'dist', 'backend'),
    note:
      'Empty until `npm run backend:freeze` populates it with the frozen\n' +
      'backend. Debug builds do not need it: the Rust shell runs\n' +
      'backend/sidecar.py from the uv virtualenv instead.\n',
  },
  {
    path: join(projectDir, 'backend', 'resource', 'bin'),
    note:
      'Holds the bundled LGPL ffmpeg.exe. Fetch it with\n' +
      '`npm run ffmpeg:fetch`; without it the backend falls back to\n' +
      'whatever FFmpeg is on PATH.\n',
  },
];

for (const { path, note } of required) {
  mkdirSync(path, { recursive: true });
  const readme = join(path, 'README.txt');
  if (!existsSync(readme)) {
    writeFileSync(readme, note, 'utf8');
  }
}
