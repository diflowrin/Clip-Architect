# Third-party notices

Clip Architect is free software, and it is built out of other people's free
software. This folder ships inside the application so that every licence which
requires its text to travel with the binary actually has it here, on the disk of
whoever installed the app.

| File | What it covers |
|---|---|
| `LICENSE.txt` | Clip Architect itself — GNU GPL version 3 |
| `LICENSE-MoneyPrinterTurbo.txt` | the upstream project this is built on — MIT |
| `FFMPEG-LICENSE.md`, `FFMPEG-BUILD.txt` | the bundled FFmpeg and what is compiled into it |
| `LGPL-3.0.txt`, `LGPL-2.1.txt` | the Lesser GPL, for FFmpeg and for edge-tts |
| `OFL-1.1-NotoSans.txt`, `OFL-1.1-BeVietnamPro.txt`, `OFL-1.1-Charm.txt` | the bundled subtitle fonts |
| `LICENSES-python.txt` | every Python package inside the frozen backend |

## Clip Architect — GPL-3.0

Copyright © 2026 the Clip Architect authors.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See `LICENSE.txt` for the full terms.

**Complete corresponding source code** is published at
<https://github.com/diflowrin/Clip-Architect>, and the project page is
<https://diflowrin.com>. If you received this application without access to
that repository, request the source through either address and it will be
provided.

The author separately offers Clip Architect under a commercial licence for use
that the GPL does not allow. That offer takes nothing away from the terms above:
the copy you have is GPL-3.0, and stays GPL-3.0.

## MoneyPrinterTurbo — MIT

The video pipeline in `backend/` derives from
[MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo), Copyright
© 2024 Harry, used under the MIT licence — a verbatim copy is in
`LICENSE-MoneyPrinterTurbo.txt`. The MIT notice continues to apply to those
portions; the combined work is distributed under the GPL-3.0 above.

## FFmpeg — LGPL v3

Rendering, encoding and muxing are done by FFmpeg, shipped as
`resource/bin/ffmpeg.exe` and invoked as a separate program.

The binary is an **unmodified** build published by
[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) — version
`n9.0.1-6-g9d4ca21220-20260822`, from the `ffmpeg-n9.0-latest-win64-lgpl-9.0.zip`
asset. It is configured **without** `--enable-gpl` and **with**
`--enable-version3`, so the binary as a whole is under the GNU Lesser General
Public License **version 3** (`LGPL-3.0.txt`, which extends the GPL-3.0 text in
`LICENSE.txt`). Most of FFmpeg is LGPL v2.1-or-later, included as
`LGPL-2.1.txt`; `FFMPEG-LICENSE.md` is FFmpeg's own statement of which parts are
under which terms, and `FFMPEG-BUILD.txt` records the exact version string and
the full list of libraries compiled into the binary that ships here.

**Source offer.** The complete corresponding source for this FFmpeg build is
available from BtbN's release page above (which also publishes the build
scripts) and from <https://github.com/FFmpeg/FFmpeg> at the tag named in
`FFMPEG-BUILD.txt`. For three years from the date you received this
application, the same source will also be provided on request through
<https://github.com/diflowrin/Clip-Architect> or <https://diflowrin.com>.

Nothing in the application modifies FFmpeg or links against it: it is executed
as a child process, and you are free to replace `resource/bin/ffmpeg.exe` with
your own build of the same version.

## Subtitle fonts — SIL Open Font License 1.1

Every face in `resource/fonts/` is under the
[SIL Open Font License 1.1](https://openfontlicense.org), whose full text —
including each font's own copyright line — is in the files listed below. Neither
font is sold, and neither is distributed under a name reserved by its authors.

| Font | Copyright | Licence text |
|---|---|---|
| Noto Sans (Bold, Medium) | © 2022 The Noto Project Authors | `OFL-1.1-NotoSans.txt` |
| Be Vietnam Pro (Bold, Medium) | © 2021 The Be Vietnam Pro Project Authors | `OFL-1.1-BeVietnamPro.txt` |
| Charm (Regular, Bold) | © 2018 The Charm Project Authors | `OFL-1.1-Charm.txt` |

## Background music

The eight tracks in `resource/songs/` were generated on a paid Suno account
whose terms assign the subscriber all of Suno's right, title and interest in the
output. They are the project's own material and carry no third-party licence.
Music you add yourself is stored in your app data folder and is never
redistributed by this application.

## Python packages

The backend is a frozen CPython 3.11 bundle, so its dependency tree is
redistributed inside the application. `LICENSES-python.txt` lists all of them
with the full text of each licence — predominantly MIT, BSD and Apache-2.0.
A few entries are worth calling out by name:

- **CPython** itself is under the Python Software Foundation License.
- **PyInstaller** produced the frozen bundle, and its bootloader is embedded in
  the backend executable. PyInstaller is GPL v2 *with an exception* that
  explicitly permits distributing bundled applications under any terms; that
  exception is what makes this bundle possible, and its text is in
  `LICENSES-python.txt`.
- **Microsoft Cognitive Services Speech SDK** (`azure-cognitiveservices-speech`)
  is proprietary Microsoft code, redistributed under the terms in
  `LICENSES-python.txt`. It is dormant unless you enter your own Azure Speech
  key; the application ships no credentials.
- **edge-tts** is under the LGPL v3 (`LGPL-3.0.txt`), used unmodified.

## Microsoft components

- **H.264 encoding** goes through `h264_mf`, a wrapper over the encoder built
  into Windows Media Foundation. That encoder is part of your operating system
  and is licensed by Microsoft; this application bundles no H.264 encoder of its
  own, and never emits HEVC.
- **WebView2** renders the user interface. `WebView2Loader.dll` is redistributed
  under Microsoft's Distributable Code terms for the WebView2 SDK; the runtime
  itself is part of Windows or installed by Microsoft's bootstrapper.

## Services contacted at runtime

The application ships no API keys and contacts nothing until you enter your own.
Your chosen LLM provider, the stock-footage APIs (Pexels, Pixabay) and the
text-to-speech engines are reached directly from your machine under the terms
you accepted when you obtained those keys. Footage retrieved from Pexels or
Pixabay stays governed by those services' own licence terms.
