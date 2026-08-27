Bundled FFmpeg.

ffmpeg.exe here is an LGPL build (BtbN/FFmpeg-Builds), fetched by
scripts/fetch-ffmpeg.ps1 and excluded from version control because of its size.

It must stay LGPL. A GPL build carries libx264/libx265, and distributing those
means distributing an AVC encoder - which brings both GPL obligations and H.264
patent licensing. The application encodes through h264_mf (Windows Media
Foundation) instead, leaving that licence with the operating system. The fetch
script refuses a build configured with --enable-gpl for exactly this reason.

The Rust shell passes this path to the backend as IMAGEIO_FFMPEG_EXE, which is
first in its resolution order, so it wins over any FFmpeg on PATH.

The build is configured with --enable-version3, so as distributed it is LGPL v3.
The fetch script records the exact version and the libraries compiled into it in
licenses/FFMPEG-BUILD.txt, which is what the source offer in
licenses/THIRD-PARTY-NOTICES.md points at. Replacing this binary by hand without
re-running the script leaves that offer describing a build nobody is shipping.
