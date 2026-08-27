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

import { mkdirSync, writeFileSync, existsSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join, posix, relative, sep } from 'node:path';
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

/**
 * Deletes what an earlier build copied and the source no longer has.
 *
 * tauri-build copies bundle.resources next to the binary on every build, but it
 * never removes anything, so a file deleted from backend/resource/ stays in
 * src-tauri/target/<profile>/ and keeps being packaged. This is not a tidiness
 * problem: twelve music tracks pulled on 2026-08-25 for unverifiable
 * provenance, and any font or model removed for the same reason, survive there
 * and go straight into the MSIX, which is built by copying that folder.
 *
 * Only files that a *directory* mapping is responsible for are considered.
 * Files placed by their own mapping - licenses/LICENSE.txt comes from the
 * repository root - are kept, since no source directory contains them.
 */
function pruneStaleResources() {
  const config = JSON.parse(
    readFileSync(join(projectDir, 'src-tauri', 'tauri.conf.json'), 'utf8'),
  );
  const resources = config.bundle?.resources ?? {};

  const directories = [];
  const placedFiles = new Set();
  for (const [source, target] of Object.entries(resources)) {
    const sourcePath = join(projectDir, 'src-tauri', source);
    const targetPath = target.replace(/[\\/]+$/, '');
    if (existsSync(sourcePath) && statSync(sourcePath).isDirectory()) {
      directories.push({ sourcePath, targetPath });
    } else {
      placedFiles.add(posix.normalize(target.split(sep).join('/')));
    }
  }

  for (const profile of ['debug', 'release']) {
    const profileDir = join(projectDir, 'src-tauri', 'target', profile);
    if (!existsSync(profileDir)) continue;

    for (const { sourcePath, targetPath } of directories) {
      const copied = join(profileDir, targetPath);
      if (!existsSync(copied)) continue;
      prune(copied, sourcePath, targetPath);
    }
  }

  function prune(copiedDir, sourceDir, targetPrefix) {
    for (const entry of readdirSync(copiedDir, { withFileTypes: true })) {
      const copied = join(copiedDir, entry.name);
      const source = join(sourceDir, entry.name);
      const asTarget = posix.join(targetPrefix, entry.name);
      if (entry.isDirectory()) {
        if (existsSync(source)) prune(copied, source, asTarget);
        else if (!placedFiles.has(asTarget)) remove(copied);
        continue;
      }
      if (!existsSync(source) && !placedFiles.has(asTarget)) remove(copied);
    }
  }

  function remove(path) {
    rmSync(path, { recursive: true, force: true });
    console.log(`removed stale bundle resource: ${relative(projectDir, path)}`);
  }
}

pruneStaleResources();
