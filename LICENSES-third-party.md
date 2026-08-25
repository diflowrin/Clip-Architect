# Third-party licenses

ClipArchitect is a desktop repackaging of
[MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo). This file
records what ships inside the installer and under what terms. It is also the
evidence you will want on hand if Microsoft Store certification asks about
bundled content.

**ClipArchitect is licensed under GPL-3.0** (see `LICENSE`).

Nothing bundled forces that choice. The application ships an **LGPL** build of
FFmpeg, so there is no copyleft obligation flowing up from a dependency; GPL-3.0
is a deliberate preference, and relicensing to something permissive would be a
licensing decision rather than an engineering one.

Publishing GPL software through the Microsoft Store is permitted, but in
Partner Center you must supply **your own licence terms** (the GPL) instead of
accepting the default Standard Application License Terms. The standard terms
impose redistribution restrictions that conflict with GPL section 10's "no
further restrictions" clause.

## Application code

| Component | Origin | License |
|---|---|---|
| Backend (`backend/app/`) | MoneyPrinterTurbo by Harry | MIT |
| Desktop shell (`src-tauri/`), frontend (`src/`) | This project | GPL-3.0 |
| Tauri | tauri-apps | MIT / Apache-2.0 |

MoneyPrinterTurbo's MIT license permits redistribution and modification,
including commercially, provided the copyright notice and license text travel
with the software. MIT is one-way compatible with GPL-3.0, so the combined work
ships under the GPL while the upstream portions keep their MIT notice - a
verbatim copy lives in `LICENSE-MoneyPrinterTurbo`. Tauri is MIT / Apache-2.0;
Apache-2.0 is compatible with GPLv3 (though not GPLv2).

**Modifications made to the upstream backend**, so they are easy to review:

- `app/paths.py` — new; resolves the data and resource directories from
  `MPT_ROOT_DIR` / `MPT_RESOURCE_DIR` so the app can install read-only.
- `app/utils/utils.py`, `app/config/config.py` — use the resolver above instead
  of deriving paths from `__file__`.
- `app/controllers/v1/settings.py` — new; exposes settings and metadata over
  HTTP, replacing the in-process configuration editing the Streamlit UI did.
- `app/router.py` — registers the settings router.
- `app/models/schema.py`, `app/services/video.py` — default subtitle font
  changed from `STHeitiMedium.ttc` to `BeVietnamPro-Bold.ttf` (see below).
- `app/services/video.py` — default video encoder changed from `libx264` to
  `h264_mf`, and the codec fallback reworked into an ordered chain. Upstream
  fell back to a hard-coded `libx264` from every failure path, which silently
  assumed a GPL FFmpeg; with the LGPL build that encoder does not exist and
  every render would fail with no way out.
- `app/services/video.py` — clips are now cut, letterboxed and encoded by a
  single FFmpeg pass (`_render_clip_with_ffmpeg`) instead of frame-by-frame in
  MoviePy. Measured at 1080x1920: MoviePy's resize plus CompositeVideoClip cost
  ~420 ms per frame, so a five-second clip took ~150 s; FFmpeg's scale and pad
  filters do it in under 4 s. MoviePy still handles clips that need a
  transition, since those require per-frame compositing.
- `app/services/video.py`, `app/services/task.py` — `combine_videos` reports
  progress per clip. That stage is most of a render's wall-clock time and
  previously ran entirely between the 50% and 75% checkpoints, so the UI sat at
  50% for the duration and looked hung.
- The Streamlit WebUI (`webui/`) and its dependencies are not shipped.

## Bundled fonts — `backend/resource/fonts/`

Both families are licensed under the
[SIL Open Font License 1.1](https://scripts.sil.org/OFL), which explicitly
permits bundling inside a commercial application.

| Font | Copyright | License |
|---|---|---|
| Be Vietnam Pro (Bold, Medium) | © 2021 The Be Vietnam Pro Project Authors | OFL-1.1 |
| Charm (Regular, Bold) | © 2018 The Charm Project Authors | OFL-1.1 |

### Removed from the upstream bundle

Upstream shipped five additional fonts that **cannot** be redistributed. They
were moved to `packaging/removed-fonts/` and are excluded from the build:

| Font | Why it was removed |
|---|---|
| Microsoft YaHei (Bold, Normal) | © Microsoft, portions © Beijing Founder Electronics. Licensed only for use with Microsoft software; redistribution in a third-party app is not granted. |
| STHeiti (Light, Medium) | © 2000–2007 Changzhou SinoType Technology, all rights reserved. No redistribution grant. |
| UTM Kabel KT | Thien Minh Co. Ltd; terms unclear and not verifiable. |

STHeitiMedium.ttc was upstream's **default** subtitle font, so the default was
changed to Be Vietnam Pro Bold.

### Script coverage after the removals

Measured against the bundled fonts, not assumed:

| Script | Covered | By |
|---|---|---|
| Latin | yes | Be Vietnam Pro, Charm |
| Vietnamese | yes | Be Vietnam Pro, Charm |
| Thai | yes | Charm |
| **Cyrillic** | **no** | — |
| **Greek** | **no** | — |
| **CJK** | **no** | — |

This is wider than it first appears: Russian, Ukrainian, Bulgarian, Serbian and
Greek subtitles are affected as well as Chinese, Japanese and Korean. A subtitle
in an uncovered script renders as blank or tofu glyphs rather than failing
loudly, because `subtitle_font_supports_text` fails open by design.

Adding coverage needs no code change — drop an OFL-licensed face (Noto Sans SC,
Noto Sans, or Source Han Sans) into `backend/resource/fonts/` and it is
discovered at startup. Update
`test_video.py::test_bundled_fonts_script_coverage`, which pins the current
limitation so it cannot shrink unnoticed.

## Bundled music — `backend/resource/songs/`

**Upstream's 29 tracks were removed.** Their own README states they came from
YouTube videos, with a request to delete them if there is a copyright claim.
That is not a redistribution grant, so they were moved to
`packaging/removed-songs/` and excluded from the build.

Twenty tracks were added afterwards, from three different sources. Tauri copies
`backend/resource/` into the package wholesale, so **everything in this folder
ships**, and every track therefore needs a redistribution grant — not merely a
right to use it in a video. Twelve of the twenty had no recorded source and were
moved to `packaging/removed-songs/unverified-provenance/` on 2026-08-25; that
folder's README lists them and what clearing one would take.

**The shipped library is now the eight Suno tracks below, and nothing else.**

### Generated on a paid Suno account — 8 tracks, the whole library

`a-symphony-in-green`, `liquid-funk`, `nightfall-echoes`, `ocean-whispers`,
`underneath-the-stars-enhanced`, `untitled-track`, `whispering-waves`,
`whispers-in-the-night` (all dated 5–17 October 2024).

Suno's terms assign Pro and Premier subscribers "all of its right, title and
interest in and to any Output owned by Suno and generated from Submissions made
by you". On that basis these are the project's own material and can be
redistributed.

Two limits are worth recording rather than discovering later:

- **The assignment is tied to the subscription being active when the track was
  generated.** Free-tier output does not carry commercial rights. October 2024
  must fall inside a paid period; the Suno account's generation history is the
  evidence, and it is worth exporting rather than relying on memory.
- **Suno warrants no copyright.** Its terms state it "makes no representation or
  warranty to you that any copyright will vest in any Output" — AI-generated
  audio may attract no copyright at all. This does not restrict use, but it
  means the project cannot warrant ownership of these tracks to a commercial
  licensee. Keep that out of any warranty clause in
  [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

### Removed — 12 tracks

Nine downloaded from free-music sites (six `mixkit-*`, plus `audiodollar-…`,
`joyinsound-…` and `oceanframemusic-…`, dated 14 June 2026) and three with no
recorded source at all (`Nocturnal Pursuit`, `Sparse Piano Motif`,
`Stillness After Rain`, dated 19–23 August 2026).

They are in `packaging/removed-songs/unverified-provenance/`, kept for reference
and excluded from the build. Moving one back requires writing its redistribution
grant into this section first. If the three August tracks turn out to be Suno
output from the same paid account, they belong in the group above.

The feature works with an empty folder: users add their own tracks through the
app, which stores them under `storage/bgm` in the app data folder, and when no
music is present "random" resolves to no background music rather than failing.
Removing a track is therefore never a functional regression.

Anything added here needs its source and licence recorded in this section at the
same time. A track with no line here is a track nobody can clear later.

## FFmpeg

Video encoding uses FFmpeg. It is resolved at runtime in this order
(`app/utils/utils.py::get_ffmpeg_binary`):

1. `IMAGEIO_FFMPEG_EXE`, or `app.ffmpeg_path` in `config.toml`
2. `ffmpeg` on `PATH`
3. the build shipped inside the `imageio-ffmpeg` package
4. the bare string `ffmpeg`

Step 1 is what ships. The Rust shell sets `IMAGEIO_FFMPEG_EXE` to
`resource/bin/ffmpeg.exe`, an **LGPL** build from
[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) (`n9.0`, configured
with `--disable-libx264 --disable-libx265`). Setting it explicitly also stops a
stray FFmpeg on the user's `PATH` from changing which codecs are available.

The GPL build that `imageio-ffmpeg` carries — `gyan.dev` "essentials",
`--enable-gpl --enable-version3`, linked against libx264/libx265 — is
**stripped from the bundle** in `sidecar.spec`. The Python module stays, because
moviepy imports it; only its binary is dropped.

### Why LGPL rather than GPL

Two problems disappear at once, and only one of them is about copyright:

- **No copyleft obligation from FFmpeg.** Nothing bundled forces the project's
  own licence, so GPL-3.0 stays a free choice.
- **No AVC patent exposure.** This is the one a licence cannot fix. Patent
  rights are independent of copyright: distributing an H.264 *encoder*
  implementation is what triggers AVC licensing (Via LA, formerly MPEG-LA),
  regardless of how that encoder is licensed. Shipping libx264 would make the
  publisher of this application the distributor of an encoder.

The application instead encodes with `h264_mf`, a thin wrapper over the H.264
encoder built into Windows Media Foundation. The encoder is part of the
operating system and licensed by Microsoft, so the app calls it rather than
distributing one.

**The trade-off is real and worth stating:** `h264_mf` emits Constrained
Baseline — no B-frames, no CABAC — where libx264 defaults to High. Expect
noticeably larger files for the same visual quality. For short videos destined
for TikTok, Reels or Shorts this matters little, since those platforms re-encode
on ingest anyway.

`libopenh264` is in the fallback chain but is **not** a patent escape hatch:
Cisco pays AVC royalties only for binaries Cisco itself distributes, and the
BtbN build links openh264 statically rather than loading Cisco's DLL. It sits
below `h264_mf` purely as a functional fallback.

### HEVC

Never emitted. `_SUPPORTED_VIDEO_CODECS` in `app/services/video.py` contains
only H.264 encoders. HEVC licensing is materially worse — three separate pools
(Access Advance, Via LA, independent licensors) with no comparable low-volume
tier — so keep it that way.

### If you ever switch back to a GPL build

`scripts/fetch-ffmpeg.ps1` refuses a build configured with `--enable-gpl`, by
design. Reverting means removing that guard, dropping the strip step in
`sidecar.spec`, and accepting both the GPL source-offer obligation and the AVC
patent question above.

## Python dependencies

The frozen backend embeds CPython 3.11 and its dependency tree (FastAPI,
uvicorn, moviepy, litellm, faster-whisper, edge-tts and their transitive
dependencies). These are predominantly MIT, BSD and Apache-2.0. Generate the
full inventory for your submission records with:

```
cd backend
uv run python -m pip install pip-licenses
uv run pip-licenses --format=markdown --with-urls > ../LICENSES-python.md
```

## Services contacted at runtime

The app ships no credentials. Each of these is used only after the user enters
their own API key in Settings, and is contacted directly from the user's
machine:

- The configured LLM provider (OpenAI, Anthropic, Gemini, DeepSeek, and others)
- Stock footage APIs (Pexels, Pixabay)
- Microsoft Edge TTS (no key required), and optionally Azure Speech or
  ElevenLabs

Footage retrieved from Pexels and Pixabay is governed by those services' own
license terms, which the end user accepts when they obtain an API key. Worth
restating in the Store listing so the attribution expectations are clear to
users.
