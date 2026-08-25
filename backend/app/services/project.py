"""
Saved projects: the settings behind a render, kept re-editable.

A finished render leaves a video, a script and an audio track on disk, but the
settings that produced it - subject, voice, subtitle styling, aspect, clip
length - lived only in the frontend form and were gone the moment the app
closed. Saving a project persists that form so a video can be reproduced or
adjusted later without retyping it.

Projects are plain JSON files under ``storage/projects``, one per project, with
no index: the directory is the listing. An index would be a second copy of the
same facts and the two drift the first time a write fails halfway.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone

from loguru import logger

from app.models.schema import VideoParams
from app.utils import utils

MAX_TITLE_LENGTH = 120
DEFAULT_TITLE = "Untitled project"
# A render uses a few dozen clips at most. The cap is there so a malformed
# payload cannot write an unbounded file, not because the limit is meaningful.
MAX_MATERIALS = 500

# Ids are generated here, never supplied by the client, so the pattern can be
# strict. Everything that builds a file path from an id checks it first.
_PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

# Fields that describe one run rather than the recipe. ``custom_audio_file``
# points inside a task directory that a later render will not have; replaying
# it would either fail or, worse, narrate the new video with the old audio.
_TRANSIENT_PARAM_FIELDS = ("custom_audio_file",)


def projects_dir() -> str:
    return utils.storage_dir("projects", create=True)


def is_valid_project_id(project_id: str) -> bool:
    return bool(_PROJECT_ID_PATTERN.match(str(project_id or "")))


def _project_file(project_id: str) -> str:
    if not is_valid_project_id(project_id):
        raise ValueError(f"invalid project id: {project_id!r}")
    return os.path.join(projects_dir(), f"{project_id}.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_title(title) -> str:
    cleaned = str(title or "").strip()
    return cleaned[:MAX_TITLE_LENGTH] or DEFAULT_TITLE


def normalize_params(params) -> dict:
    """
    Validate a params payload against the same model the render endpoints take.

    Storing the validated dump rather than the raw body means a project can
    always be posted straight back to /videos: unknown keys are dropped and
    enums are stored as their string values, so the file survives a round trip
    through JSON without turning into something the API would reject.
    """
    if not isinstance(params, dict):
        raise ValueError("project params must be an object")

    # ``warnings=False``: several VideoParams fields declare their default as
    # the enum's *value* rather than the member, so a field left at its default
    # holds a plain string and pydantic warns that it expected an enum. The dump
    # is correct either way - both paths produce the string - and the warning
    # would otherwise fire on every save.
    validated = VideoParams(**params).model_dump(mode="json", warnings=False)
    for field in _TRANSIENT_PARAM_FIELDS:
        validated.pop(field, None)
    return validated


def normalize_materials(materials) -> list[str]:
    """
    The clip list of the render this project was saved from, in timeline order.

    Kept so re-editing can reopen the same footage instead of matching new
    clips against the script - the whole point of coming back to a project is
    usually the arrangement, not another roll of the dice. These are untrusted
    on the way back in: the reopen endpoint resolves every one of them against
    the directories a task may read from before anything is played or rendered.
    """
    if materials is None:
        return []
    if not isinstance(materials, list):
        raise ValueError("project materials must be a list of paths")

    cleaned = [
        item.strip()
        for item in materials
        if isinstance(item, str) and item.strip()
    ]
    return cleaned[:MAX_MATERIALS]


def _summary(record: dict) -> dict:
    """Listing shape: enough to identify a project without its whole payload."""
    params = record.get("params") or {}
    return {
        "id": record.get("id"),
        "title": record.get("title"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "task_id": record.get("task_id"),
        "video_subject": params.get("video_subject", ""),
        "clip_count": len(record.get("materials") or []),
    }


def _write_record(record: dict) -> None:
    """
    Write a project, replacing any previous version in one step.

    A project is small, but it is also the only copy of the settings. Writing
    to a temporary file and renaming means a crash mid-write leaves the old
    project intact instead of a truncated file that no longer parses.
    """
    target = _project_file(record["id"])
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, target)


def _read_record(project_id: str) -> dict | None:
    try:
        with open(_project_file(project_id), encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning(f"cannot read project {project_id}: {exc}")
        return None
    if not isinstance(record, dict):
        return None
    # Projects saved before clips were stored have no key for them.
    record.setdefault("materials", [])
    return record


def save_project(
    title, params, task_id: str | None = None, materials=None
) -> dict:
    """Create a project from a params payload. Raises ValueError on bad input."""
    record = {
        "id": uuid.uuid4().hex,
        "title": normalize_title(title),
        "created_at": _now(),
        "updated_at": _now(),
        # The render this was saved from. Unlike the reference it used to be,
        # this is now load-bearing: reopening the saved clips needs the task
        # directory they live in, and its narration audio to render against.
        "task_id": str(task_id) if task_id else None,
        "params": normalize_params(params),
        "materials": normalize_materials(materials),
    }
    _write_record(record)
    logger.info(f"saved project '{record['title']}' ({record['id']})")
    return record


def list_projects() -> list[dict]:
    """All saved projects, newest first. Unreadable files are skipped."""
    directory = projects_dir()
    summaries = []
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        project_id = name[: -len(".json")]
        if not is_valid_project_id(project_id):
            continue
        record = _read_record(project_id)
        if record:
            summaries.append(_summary(record))

    summaries.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return summaries


def load_project(project_id: str) -> dict | None:
    return _read_record(project_id)


def update_project(
    project_id: str, title=None, params=None, task_id=None, materials=None
) -> dict | None:
    """
    Update a project in place. Returns None when it no longer exists.

    Every field is optional so the callers - renaming from the library, and
    re-saving edited settings or a re-rendered clip list from the form - send
    only what they changed. An omitted field keeps its stored value; passing an
    empty list for ``materials`` is how a caller clears it on purpose.
    """
    record = _read_record(project_id)
    if record is None:
        return None

    if title is not None:
        record["title"] = normalize_title(title)
    if params is not None:
        record["params"] = normalize_params(params)
    if task_id is not None:
        record["task_id"] = str(task_id) or None
    if materials is not None:
        record["materials"] = normalize_materials(materials)
    record["updated_at"] = _now()
    record["id"] = project_id
    _write_record(record)
    return record


def delete_project(project_id: str) -> bool:
    try:
        os.remove(_project_file(project_id))
    except FileNotFoundError:
        return False
    return True
