# PyInstaller spec for the backend sidecar.
#
# Built as a one-DIRECTORY bundle, not one-file, on purpose:
#
#   * one-file re-extracts the whole payload into %TEMP% on every launch, which
#     adds seconds to startup and reliably trips antivirus heuristics.
#   * MSIX (Microsoft Store) installs read-only and expects a static, already
#     laid-out payload. A directory bundle maps onto that directly.
#
# The output directory is shipped as a Tauri *resource*, not an externalBin,
# because externalBin only carries a single loose executable.
#
# Build with:  uv run pyinstaller sidecar.spec --noconfirm

import os

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# Azure voice catalogue is read at runtime by app/services/voice.py.
datas = [("app/services/data/azure_voices.json", "app/services/data")]

# faster-whisper and edge-tts resolve model metadata and voice lists from data
# files that the import scan does not see.
datas += collect_data_files("faster_whisper")
datas += collect_data_files("edge_tts")

# Packages that call importlib.metadata.version() on themselves at import time
# need their .dist-info shipped, or they raise PackageNotFoundError inside the
# frozen build. imageio does this in its __init__, which moviepy imports, so
# missing it takes down the whole backend at startup.
for _distribution in ("imageio", "imageio-ffmpeg", "moviepy", "litellm", "openai"):
    try:
        datas += copy_metadata(_distribution)
    except Exception as exc:  # noqa: BLE001 - optional dependency
        print(f"spec: no metadata for {_distribution}: {exc}")

hiddenimports = [
    # uvicorn selects its loop/protocol implementations by string name.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # Provider adapters are imported lazily from app/services/llm.py, so the
    # scan only reaches them if they are named here.
    "openai",
    "google.genai",
    "dashscope",
    "azure.cognitiveservices.speech",
]

# Deliberately NOT using collect_submodules("litellm"): it imports every
# submodule of a very large package, which adds minutes to the build and pulls
# optional SDKs (vertexai, bedrock, ...) into the bundle. Exactly one provider
# in app/models/llm_provider.py routes through litellm; every mainstream
# provider uses the OpenAI-compatible adapter instead. The normal import scan
# covers litellm.completion, which is all that provider calls.

a = Analysis(
    ["sidecar.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # The Streamlit WebUI is replaced by the React frontend.
        "streamlit",
        # Pulled in transitively but never used by the backend; each drags in a
        # sizeable native or UI stack of its own.
        "tkinter",
        "matplotlib",
        "IPython",
        "notebook",
        "pytest",
    ],
    noarchive=False,
)

# imageio-ffmpeg's PyInstaller hook bundles the FFmpeg binary that ships inside
# the package - a gyan.dev build configured with --enable-gpl and linked against
# libx264/libx265. This application ships its own LGPL FFmpeg under
# resource/bin instead, so that binary is dropped here: leaving it in would put
# a GPL encoder in the package and, with it, an AVC patent obligation that using
# the operating system's encoder avoids. The Python module itself is kept,
# because moviepy imports it.
#
# See LICENSES-third-party.md before changing this.
#
# The entry is checked against BOTH a.datas and a.binaries: imageio-ffmpeg's
# hook collects it with collect_data_files, but PyInstaller reclassifies
# executables into a.binaries, so filtering only datas silently does nothing.


def _is_bundled_ffmpeg(entry):
    # Match on source and destination: the hook records a destination like
    # "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe", but analysis passes
    # can rewrite either field.
    paths = " ".join(str(field).replace("\\", "/").lower() for field in entry[:2])
    return "imageio_ffmpeg" in paths and "ffmpeg" in paths and paths.count(".exe") > 0


_removed_ffmpeg = [entry for entry in a.datas if _is_bundled_ffmpeg(entry)]
_removed_ffmpeg += [entry for entry in a.binaries if _is_bundled_ffmpeg(entry)]
a.datas = [entry for entry in a.datas if not _is_bundled_ffmpeg(entry)]
a.binaries = [entry for entry in a.binaries if not _is_bundled_ffmpeg(entry)]

for _entry in _removed_ffmpeg:
    print(f"spec: excluded GPL ffmpeg binary from bundle: {_entry[0]}")

# Independent guard, deliberately using a broader and simpler predicate than the
# filter above. If the hook ever changes shape so the filter stops matching, this
# still catches it - otherwise the package would quietly start shipping a GPL
# encoder again, which is the exact failure this whole block exists to prevent.
_leaked_ffmpeg = [
    entry[0]
    for entry in list(a.datas) + list(a.binaries)
    if os.path.basename(str(entry[0])).lower().startswith("ffmpeg")
    and str(entry[0]).lower().endswith(".exe")
]
if _leaked_ffmpeg:
    raise SystemExit(
        "spec: an FFmpeg executable is still in the bundle: "
        f"{_leaked_ffmpeg}. The application ships its own LGPL build under "
        "resource/bin; see LICENSES-third-party.md."
    )
print(f"spec: verified no FFmpeg executable is bundled ({len(_removed_ffmpeg)} removed)")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ca-desktop-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Console subsystem, deliberately. A windowed build leaves sys.stdout as
    # None, which breaks app/config/__init__.py where loguru is attached to it.
    # The Rust shell passes CREATE_NO_WINDOW when spawning, so no window ever
    # appears while stdout still flows to the parent for logging.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="backend",
)
