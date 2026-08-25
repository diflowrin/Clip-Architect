Background music library.

Everything in this folder SHIPS. Tauri copies backend/resource/ into the package
wholesale, so a track here is redistributed to every user - which needs a
redistribution grant, not merely a right to use the track in a video. Those are
different permissions, and most "free music" sites grant only the second.

Upstream MoneyPrinterTurbo shipped 29 MP3 files here. Its own README states they
were taken from YouTube videos and asks anyone with a copyright claim to delete
them, which means their redistribution rights were never cleared. They cannot
ship in a Microsoft Store package, so they were removed; they are kept in
packaging/removed-songs/ for reference only.

The 8 tracks here were generated on a paid Suno account, whose terms assign the
subscriber ownership of the output, so they are the project's own material and
may be redistributed. Twelve others were removed on 2026-08-25 because no source
was ever recorded for them; they are in
packaging/removed-songs/unverified-provenance/. Details in
LICENSES-third-party.md under "Bundled music".

When you add a track, record its source and licence in that section at the same
time. A file with no line there is one nobody can clear later, and the safe
resolution is always deletion.

Users add their own music through the app, which stores uploads under
storage/bgm in the app data folder. Both directories are searched at runtime,
and when no music is present "random" resolves to no background music rather
than failing - so removing a track is never a functional regression.
