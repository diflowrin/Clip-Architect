"""Settings and metadata endpoints for the desktop frontend.

Upstream's Streamlit WebUI ran inside the backend process and mutated
``app.config.config`` dictionaries directly. The desktop frontend is a separate
web app talking over HTTP, so the configuration it needs to render and persist
has to be exposed as an API.

Only the sections that ``save_config`` round-trips are writable; anything else
would be silently dropped on the next save, which is worse than refusing it.
"""

import os

from fastapi import Depends, Path, Request
from fastapi.responses import FileResponse
from loguru import logger

from app.config import config
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.llm_provider import LLM_PROVIDER_REGISTRY, get_llm_provider
from app.services import elevenlabs_music, llm_catalog, sonilo
from app.services import voice as voice_service
from app.utils import file_security, utils

router = new_router(dependencies=[Depends(base.verify_token)])

# save_config() rebuilds config.toml from exactly these sections. Accepting a
# key outside the list would look like it worked until the next restart.
_WRITABLE_SECTIONS = {
    "app": config.app,
    "azure": config.azure,
    "siliconflow": config.siliconflow,
    "minimax_tts": config.minimax_tts,
    "elevenlabs": config.elevenlabs,
    "chatterbox": config.chatterbox,
    "ui": config.ui,
}

_FONT_EXTENSIONS = (".ttf", ".ttc", ".otf")

# What the Settings form actually edits, per section. Everything else is
# refused - but only when the request would really change it, see below.
#
# The list exists because config.toml is not inert data. ``app.ffmpeg_path``
# names a binary the render pipeline executes on every task, ``app.tls_verify``
# turns off certificate validation on every outbound call carrying an API key,
# and the redis keys point the task manager at a host of the caller's choosing.
# A settings endpoint that took any key it was handed made all three writable
# over HTTP, which turned "save settings" into arbitrary code execution at the
# next launch. They stay editable by hand in config.toml, where changing them
# is a deliberate act by whoever owns the machine.
#
# The provider keys are derived rather than listed: the registry already
# defines the naming (``<provider_id>_<suffix>``) and drives which inputs the
# form renders, so a provider added upstream is writable without touching this
# file - and one removed stops being writable on its own.
_FIXED_APP_KEYS = frozenset(
    {
        "llm_provider",
        "subtitle_provider",
        # The stock footage key lists, edited as one textarea each.
        "pexels_api_keys",
        "pixabay_api_keys",
        # AI-composed background music. ElevenLabs Music shares the key in
        # the elevenlabs section; Sonilo's lives in app, as upstream put it.
        "sonilo_api_key",
    }
)


def _writable_app_keys() -> frozenset:
    keys = set(_FIXED_APP_KEYS)
    for provider in LLM_PROVIDER_REGISTRY:
        for suffix in ("api_key", "model_name", "base_url"):
            keys.add(provider.config_key(suffix))
        for field in provider.extra_fields:
            keys.add(provider.config_key(field.config_suffix))
    return frozenset(keys)


# The credential sections are small and stable, so they are listed outright
# rather than derived. test_settings_allowlist_matches_the_shipped_example
# pins them against config.example.toml so the two cannot drift.
_WRITABLE_KEYS = {
    "app": _writable_app_keys(),
    "azure": frozenset({"speech_key", "speech_region"}),
    "siliconflow": frozenset({"api_key"}),
    "minimax_tts": frozenset(
        {
            "api_key",
            "base_url",
            "model_id",
            "voice_id",
            "sample_rate",
            "bitrate",
            "audio_format",
            "channel",
            "pitch",
        }
    ),
    "elevenlabs": frozenset(
        {"api_key", "model_id", "music_model_id", "music_timeout"}
    ),
    "chatterbox": frozenset({"api_key", "base_url", "model_id", "voices"}),
    "ui": frozenset({"hide_log", "open_task_folder_on_completion"}),
}

# Scalars TOML round-trips, plus flat lists of them (the footage key lists and
# chatterbox's voices). A nested table would serialise into a shape the form
# cannot render and save_config cannot round-trip, so it is refused here rather
# than corrupting config.toml. bool is a subclass of int and needs no entry.
_ALLOWED_VALUE_TYPES = (str, bool, int, float)

# Distinguishes "absent" from "present and falsy": a key whose stored value is
# "" or False is unchanged when the form posts the same back, and must not be
# mistaken for a new write.
_MISSING = object()


def _is_supported_value(value) -> bool:
    if isinstance(value, _ALLOWED_VALUE_TYPES):
        return True
    if isinstance(value, list):
        return all(isinstance(item, _ALLOWED_VALUE_TYPES) for item in value)
    return False


def _read_settings() -> dict:
    settings = {name: dict(section) for name, section in _WRITABLE_SECTIONS.items()}
    # whisper and proxy are plain dicts loaded at import time. They are read-only
    # here because save_config() does not persist them.
    settings["whisper"] = dict(config.whisper)
    settings["proxy"] = dict(config.proxy)
    return settings


@router.get("/settings", summary="Read the persisted application settings")
def get_settings(request: Request):
    return utils.get_response(200, _read_settings())


@router.put("/settings", summary="Update and persist application settings")
def update_settings(request: Request, body: dict):
    request_id = base.get_task_id(request)

    if not isinstance(body, dict) or not body:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: request body must be a non-empty object",
        )

    unknown_sections = sorted(set(body) - set(_WRITABLE_SECTIONS))
    if unknown_sections:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=(
                f"{request_id}: unsupported settings sections: "
                f"{', '.join(unknown_sections)}"
            ),
        )

    for section_name, values in body.items():
        if not isinstance(values, dict):
            raise HttpException(
                task_id=request_id,
                status_code=400,
                message=f"{request_id}: section '{section_name}' must be an object",
            )

    # The form reads a whole section and posts it back, so most of what arrives
    # is the config's own current value echoed unchanged. Only what differs is
    # an edit, and only an edit needs permission - checking every key that came
    # in would make saving impossible on any config holding a key the form does
    # not edit, which is every real config.
    changes = {}
    protected = []
    malformed = []

    for section_name, values in body.items():
        section = _WRITABLE_SECTIONS[section_name]
        writable_keys = _WRITABLE_KEYS[section_name]
        section_changes = {}

        for key, value in values.items():
            current = section.get(key, _MISSING)
            if current is not _MISSING and current == value:
                continue
            if key not in writable_keys:
                protected.append(f"{section_name}.{key}")
                continue
            if not _is_supported_value(value):
                malformed.append(f"{section_name}.{key}")
                continue
            section_changes[key] = value

        if section_changes:
            changes[section_name] = section_changes

    # Refused, not ignored. A settings write that reports success and quietly
    # drops half of what it was given is the failure mode this endpoint was
    # written to avoid; the caller has to learn which key it cannot have.
    if protected:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=(
                f"{request_id}: these settings cannot be changed through the "
                f"API, edit config.toml directly: {', '.join(sorted(protected))}"
            ),
        )
    if malformed:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=(
                f"{request_id}: unsupported value type for: "
                f"{', '.join(sorted(malformed))}"
            ),
        )

    # Hold the runtime lock for the whole write so a generation task cannot read
    # a half-applied provider/key combination, matching the WebUI's behaviour.
    with config.runtime_config_lock():
        for section_name, values in changes.items():
            _WRITABLE_SECTIONS[section_name].update(values)
        config.save_config()

    return utils.get_response(200, _read_settings())


@router.get("/metadata/fonts", summary="List the bundled subtitle fonts")
def get_fonts(request: Request):
    font_directory = utils.font_dir()
    fonts = sorted(
        entry
        for entry in os.listdir(font_directory)
        if entry.lower().endswith(_FONT_EXTENSIONS)
        and os.path.isfile(os.path.join(font_directory, entry))
    )
    return utils.get_response(200, {"fonts": fonts})


# Browsers will not load a .ttc collection, so the preview falls back to its
# system stack for those. Serving them anyway would trade a wrong-but-legible
# frame for an empty one.
_WEB_FONT_MEDIA_TYPES = {
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


@router.get(
    "/metadata/fonts/{file_name}",
    summary="Serve one bundled font file, for the subtitle preview",
)
def get_font_file(
    request: Request, file_name: str = Path(..., description="Bundled font file name")
):
    """
    Hand the frontend the actual font file.

    The form let people choose a face and then drew the preview in a system
    sans, so the one thing a preview is for - seeing it before it costs a
    render - did not hold for the font. The list endpoint above returns names
    the renderer resolves against this same directory; this returns the bytes
    behind one of those names.
    """
    request_id = base.get_task_id(request)
    font_directory = utils.font_dir()
    try:
        font_path = file_security.resolve_path_within_directory(
            font_directory, file_name
        )
    except ValueError as exc:
        logger.warning(
            f"reject unsafe font path, request_id: {request_id}, "
            f"path: {file_name}, error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=404 if str(exc) == "file does not exist" else 403,
            message=f"{request_id}: invalid font path",
        )

    media_type = _WEB_FONT_MEDIA_TYPES.get(os.path.splitext(font_path)[1].lower())
    if not media_type:
        raise HttpException(
            task_id=request_id,
            status_code=404,
            message=f"{request_id}: not a font a browser can load",
        )

    return FileResponse(
        font_path,
        media_type=media_type,
        # The bundled fonts do not change between releases and each is a few
        # hundred kilobytes, which is worth not refetching on every keystroke
        # in the size box.
        headers={"Cache-Control": "max-age=86400"},
    )


# The speech engines the frontend can offer. Each needs a different answer to
# "what voices are there", and two of them need credentials, so the picker has
# to know which one it is showing rather than presenting one merged list.
_VOICE_ENGINES = ("edge", "azure", "elevenlabs")


@router.get("/metadata/voices", summary="List the voices of one speech engine")
def get_voices(request: Request, locale: str = "", engine: str = "edge"):
    """
    Voices for one engine, plus whether that engine can actually be used.

    ``configured`` and ``message`` exist because an empty dropdown does not
    say why it is empty. A missing key, a rejected key and an unreachable
    service all look identical otherwise, and the user is left setting a
    credential that was never the problem.

    ``locale`` filters Edge voices only. Azure's V2 voices and ElevenLabs's
    are multilingual, and there are few enough of either to show them all.
    """
    engine = (engine or "edge").strip().lower()
    if engine not in _VOICE_ENGINES:
        request_id = base.get_task_id(request)
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=(
                f"{request_id}: unknown voice engine '{engine}'; "
                f"expected one of {', '.join(_VOICE_ENGINES)}"
            ),
        )

    if engine == "elevenlabs":
        api_key = voice_service.get_elevenlabs_api_key()
        voices, message = voice_service.fetch_elevenlabs_voices(api_key)
        return utils.get_response(
            200,
            {
                "voices": voices,
                "engine": engine,
                "configured": bool(api_key),
                "message": message,
            },
        )

    if engine == "azure":
        configured = voice_service.azure_speech_configured()
        return utils.get_response(
            200,
            {
                "voices": voice_service.get_azure_v2_voices(),
                "engine": engine,
                "configured": configured,
                "message": (
                    ""
                    if configured
                    else "Set the Azure speech key and region in Settings."
                ),
            },
        )

    # The WebUI filters by locale prefix (e.g. "zh-CN"); an empty value returns
    # the full catalogue.
    filter_locals = [locale] if locale else None
    voices = voice_service.get_edge_voices(filter_locals=filter_locals)
    return utils.get_response(
        200,
        {"voices": voices, "engine": engine, "configured": True, "message": ""},
    )


@router.get("/metadata/llm-providers", summary="List the supported LLM providers")
def get_llm_providers(request: Request):
    # The frontend renders credential inputs from this, so it needs more than
    # a label: providers differ in whether they take an API key at all (Ollama
    # does not), and several need extra fields beyond key/base URL/model
    # (Cloudflare wants an account and gateway id). Deriving the form from the
    # registry keeps it correct as providers are added upstream.
    providers = [
        {
            "provider_id": provider.provider_id,
            "label": provider.default_label,
            "api_key_url": provider.api_key_url,
            "default_model": provider.default_model,
            "default_base_url": provider.default_base_url,
            "show_api_key": provider.show_api_key,
            "show_base_url": provider.show_base_url,
            "requires_api_key": provider.requires_api_key,
            "requires_model_name": provider.requires_model_name,
            "requires_base_url": provider.requires_base_url,
            # Whether /metadata/llm-models has anything to say about this
            # provider. The form asks before offering a model picker rather
            # than calling and hoping, so a provider without a catalogue never
            # pays for a request that can only come back empty.
            "has_model_catalog": bool(provider.model_catalog_path),
            "extra_fields": [
                {
                    # Config keys are "<provider_id>_<suffix>"; the frontend
                    # composes them the same way it does for the standard ones.
                    "config_suffix": field.config_suffix,
                    "required": field.required,
                    "secret": field.secret,
                    "default_value": field.default_value,
                }
                for field in provider.extra_fields
            ],
        }
        for provider in LLM_PROVIDER_REGISTRY
    ]
    return utils.get_response(200, {"providers": providers})


@router.get(
    "/metadata/llm-models",
    summary="List the models an LLM provider publishes",
)
def get_llm_models(request: Request, provider: str = ""):
    """
    The catalogue behind the model picker.

    Fetched here rather than by the frontend so the request follows the proxy
    and TLS settings the rest of the app uses, and so the desktop shell needs
    no allowlist entry for a provider's domain.

    A provider that publishes no catalogue answers with an empty list, and so
    does one that could not be reached - with ``error`` saying why. Neither is
    a failure: the model id is a text field underneath, and typing one is a
    supported way to use any provider, including for a model too new or too
    private to appear in a catalogue at all.
    """
    request_id = base.get_task_id(request)
    provider_id = (provider or "").strip().lower()
    if get_llm_provider(provider_id) is None:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=(
                f"{request_id}: unsupported llm provider: "
                f"{provider_id or '<empty>'}"
            ),
        )

    catalog = llm_catalog.list_models(provider_id)
    return utils.get_response(
        200,
        {
            "provider_id": provider_id,
            "models": catalog["models"],
            "error": catalog["error"],
        },
    )


# Services that compose a soundtrack for the finished cut. Their ids are the
# bgm_type values the task layer already understands.
_MUSIC_GENERATORS = (
    ("sonilo", "Sonilo", sonilo),
    ("elevenlabs", "ElevenLabs Music", elevenlabs_music),
)


@router.get(
    "/metadata/music-generators",
    summary="List the AI music services and whether each has a key",
)
def get_music_generators(request: Request):
    """
    Which AI music services the form can offer.

    Only whether a key is set - not whether it works. Checking would spend a
    request against a paid account every time the form opens, and a key that
    is refused is reported where it fails, on the render, as a warning beside
    the video.
    """
    return utils.get_response(
        200,
        {
            "generators": [
                {"id": generator_id, "label": label, "configured": service.is_enabled()}
                for generator_id, label, service in _MUSIC_GENERATORS
            ]
        },
    )
