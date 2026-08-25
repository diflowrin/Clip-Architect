import glob
import hashlib
import os
import pathlib
import shutil
import subprocess
from typing import Union
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, Path, Query, Request, UploadFile
from fastapi.params import File
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

from app.config import config
from app.controllers import base
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.controllers.manager.redis_manager import RedisTaskManager
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    AudioRequest,
    BgmRetrieveResponse,
    BgmUploadResponse,
    DownloadedMaterialRetrieveResponse,
    RenderReviewedRequest,
    ReopenTaskRequest,
    SubtitleRequest,
    TaskDeletionResponse,
    TaskListResponse,
    TaskQueryRequest,
    TaskQueryResponse,
    TaskResponse,
    TaskVideoRequest,
    VideoMaterialUploadResponse,
    VideoMaterialRetrieveResponse
)
from app.services import bgm as bgm_service
from app.services import material_upload as material_upload_service
from app.services import state as sm
from app.services import task as tm
from app.services import task_artifacts
from app.utils import file_security, utils

# 统一在 V1 视频路由入口执行鉴权。verify_token 会在 api_key 为空时
# 保留现有免认证行为，只有管理员显式配置后才会影响客户端。
router = new_router(dependencies=[Depends(base.verify_token)])

_enable_redis = config.app.get("enable_redis", False)
_redis_host = config.app.get("redis_host", "localhost")
_redis_port = config.app.get("redis_port", 6379)
_redis_db = config.app.get("redis_db", 0)
_redis_password = config.app.get("redis_password", None)
_max_concurrent_tasks = config.app.get("max_concurrent_tasks", 5)
_max_queued_tasks = config.app.get("max_queued_tasks", 100)

redis_url = f"redis://:{_redis_password}@{_redis_host}:{_redis_port}/{_redis_db}"
# 根据配置选择合适的任务管理器
if _enable_redis:
    task_manager = RedisTaskManager(
        max_concurrent_tasks=_max_concurrent_tasks,
        redis_url=redis_url,
        max_queued_tasks=_max_queued_tasks,
    )
else:
    task_manager = InMemoryTaskManager(
        max_concurrent_tasks=_max_concurrent_tasks,
        max_queued_tasks=_max_queued_tasks,
    )


def _sanitize_upload_filename(filename: str, request_id: str) -> str:
    # 浏览器或客户端有时会附带目录信息，甚至可能夹带 ../ 这类穿越片段。
    # 这里只保留纯文件名，避免上传接口把文件写到目标目录之外。
    normalized_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not normalized_name or normalized_name in {".", ".."}:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: invalid filename",
        )
    return normalized_name


def _resolve_path_within_directory(base_dir: str, unsafe_path: str, request_id: str) -> str:
    try:
        return file_security.resolve_path_within_directory(base_dir, unsafe_path)
    except ValueError as exc:
        logger.warning(
            f"reject unsafe file path, request_id: {request_id}, path: {unsafe_path}, "
            f"error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=404 if str(exc) == "file does not exist" else 403,
            message=f"{request_id}: invalid file path",
        )


def _public_task_data(task: dict) -> dict:
    """复制任务状态并移除仅用于服务端进程协调的内部字段。"""
    public_task = dict(task)
    public_task.pop("cross_post_owner", None)
    return public_task


def _task_file_to_uri(file: str, endpoint: str, task_dir: str, request_id: str) -> str:
    if not isinstance(file, str):
        return file

    if file.startswith(("http://", "https://")):
        return file

    try:
        resolved_path = file_security.resolve_path_within_directory(task_dir, file)
    except ValueError as exc:
        # 任务状态理论上只应保存任务目录内的产物路径。这里不再继续拼接 URL，
        # 避免把异常路径包装成可访问链接；同时保留原值，便于排查历史脏数据。
        logger.warning(
            f"skip unsafe task output path, request_id: {request_id}, path: {file}, "
            f"error: {str(exc)}"
        )
        return file

    relative_path = os.path.relpath(resolved_path, task_dir).replace("\\", "/")
    uri_path = f"tasks/{relative_path}"
    if endpoint:
        return f"{endpoint.rstrip('/')}/{uri_path}"
    return f"/{uri_path}"


def _material_preview_uri(material_path: str, endpoint: str) -> str | None:
    """
    Playback URI for a clip in the shared download cache or the uploads folder.

    Only a review pass forces downloads into the task directory; a render
    straight through leaves its footage in the cache, where one file is shared
    by every task that matched it rather than copied per render. Both places
    are served by ``/material_preview`` already - this is what points a clip
    list at it.
    """
    for base_dir in _material_root_dirs():
        try:
            resolved = file_security.resolve_path_within_directory(
                base_dir, material_path
            )
        except ValueError:
            continue
        relative = os.path.relpath(resolved, base_dir).replace("\\", "/")
        # Only the segments are escaped, the way the thumbnail route expects,
        # so a name with a space cannot cut the path short.
        quoted = "/".join(quote(segment, safe="") for segment in relative.split("/"))
        uri_path = f"api/v1/material_preview/{quoted}"
        if endpoint:
            return f"{endpoint.rstrip('/')}/{uri_path}"
        return f"/{uri_path}"
    return None


def _streamable_materials(material_paths: list, endpoint: str, tasks_dir: str) -> list:
    """
    Playback URIs for a clip list, aligned one-to-one with the paths given.

    A clip sits in one of two places and each has a route of its own: the task
    directory, which is where a review pass forces its downloads so the review
    screen can stream them, or the shared cache and uploads folder, which is
    where a render straight through leaves them. Both are previewable, and
    reading only the first is what made the clips of a video rendered without
    review show as "no preview" while the same footage played fine after a
    review pass.

    A path in neither is reported as null and nothing else: this runs for every
    clip of every poll while a task is open, so it cannot afford to write a log
    line per clip per second, and the null already tells the UI what to say.

    Shared by the task response and by reopening a saved project, so a clip
    previews the same way whichever screen asked for it.
    """
    streamable = []
    for material_path in material_paths:
        # Remote footage the pipeline never wrote to disk. Nothing here gets it
        # past the webview's content policy, so it has no preview either.
        if not isinstance(material_path, str) or material_path.startswith(
            ("http://", "https://")
        ):
            streamable.append(None)
            continue

        try:
            resolved = file_security.resolve_path_within_directory(
                tasks_dir, material_path
            )
        except ValueError:
            streamable.append(_material_preview_uri(material_path, endpoint))
            continue

        relative = os.path.relpath(resolved, tasks_dir).replace("\\", "/")
        uri_path = f"tasks/{relative}"
        streamable.append(
            f"{endpoint.rstrip('/')}/{uri_path}" if endpoint else f"/{uri_path}"
        )
    return streamable


def _parse_byte_range(
    range_header: str | None, file_size: int, request_id: str
) -> tuple[int, int]:
    """解析单段 HTTP Range，并把无效或越界请求稳定转换成 416。"""
    if file_size <= 0:
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        )

    if not range_header:
        return 0, file_size - 1

    try:
        # 视频播放器这里只需要单段 bytes range。拒绝多段请求可以避免返回体
        # 与 Content-Range 不一致，也避免异常字符串落入 int() 产生 500。
        if not range_header.startswith("bytes=") or "," in range_header:
            raise ValueError("unsupported range format")
        start_text, end_text = range_header[6:].split("-", 1)
        if not start_text and not end_text:
            raise ValueError("empty range")

        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError("invalid suffix length")
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
            if start < 0 or start >= file_size or end < start:
                raise ValueError("range outside file")
            end = min(end, file_size - 1)
    except (TypeError, ValueError) as exc:
        logger.warning(
            f"reject invalid video range, request_id: {request_id}, "
            f"range: {range_header}, file_size: {file_size}, error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        ) from exc

    return start, end


@router.post("/videos", response_model=TaskResponse, summary="Generate a short video")
def create_video(
    background_tasks: BackgroundTasks, request: Request, body: TaskVideoRequest
):
    return create_task(request, body, stop_at="video")


@router.post(
    "/materials",
    response_model=TaskResponse,
    summary="Prepare materials and stop for review",
)
def create_materials(
    background_tasks: BackgroundTasks, request: Request, body: TaskVideoRequest
):
    """
    Run the pipeline up to and including material selection, then stop.

    The task completes holding the clip list, script and narration; the client
    reviews the clips and calls /tasks/{task_id}/render with the approved set.
    """
    return create_task(request, body, stop_at="materials")


@router.post("/subtitle", response_model=TaskResponse, summary="Generate subtitle only")
def create_subtitle(
    background_tasks: BackgroundTasks, request: Request, body: SubtitleRequest
):
    return create_task(request, body, stop_at="subtitle")


@router.post("/audio", response_model=TaskResponse, summary="Generate audio only")
def create_audio(
    background_tasks: BackgroundTasks, request: Request, body: AudioRequest
):
    return create_task(request, body, stop_at="audio")


def _material_root_dirs() -> list:
    """
    Every directory the app manages source clips in.

    The list moved to ``utils`` once the render pipeline needed it too: local
    materials can now name a clip in the download cache, so the preview route,
    the reviewed-render validation and ``preprocess_video`` all have to accept
    the same set or a clip one of them allows fails in another.
    """
    return utils.material_root_dirs()


def _resolve_reviewed_materials(
    task_id: str, materials: list, request_id: str
) -> list:
    """
    Resolve an approved clip list against the directories it may come from.

    The client sends these paths back after review, so they are untrusted no
    matter that the backend produced them a moment earlier. Downloaded clips
    live in the task directory and uploads in storage/local_videos; anything
    resolving outside both is rejected rather than composited into a video.
    """
    allowed_dirs = [utils.task_dir(task_id), *_material_root_dirs()]
    resolved = []
    for material_path in materials:
        for base_dir in allowed_dirs:
            try:
                resolved.append(
                    file_security.resolve_path_within_directory(
                        base_dir, material_path
                    )
                )
                break
            except ValueError:
                continue
        else:
            logger.warning(
                f"reject material outside the allowed directories, "
                f"request_id: {request_id}, path: {material_path}"
            )
            raise HttpException(
                task_id=task_id,
                status_code=403,
                message=f"{request_id}: invalid material path",
            )
    return resolved


@router.post(
    "/tasks/{task_id}/render",
    response_model=TaskResponse,
    summary="Render a reviewed task from its approved materials",
)
def render_reviewed_task(
    background_tasks: BackgroundTasks,
    request: Request,
    body: RenderReviewedRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """
    Finish a task that stopped at the materials stage.

    Only the render runs: script, narration and subtitles are reused from what
    the materials phase already wrote, so nothing is generated - or paid for -
    a second time.
    """
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if not task:
        raise HttpException(
            task_id=task_id, status_code=404, message=f"{request_id}: task not found"
        )
    if not body.materials:
        raise HttpException(
            task_id=task_id,
            status_code=400,
            message=f"{request_id}: no materials were approved",
        )

    materials = _resolve_reviewed_materials(task_id, body.materials, request_id)

    try:
        task_manager.add_task(
            tm.render_reviewed_materials,
            task_id=task_id,
            params=body,
            materials=materials,
        )
    except TaskQueueFullError as e:
        logger.warning(
            f"reject render because queue is full, request_id: {request_id}, "
            f"task_id: {task_id}"
        )
        raise HttpException(
            task_id=task_id, status_code=429, message=f"{request_id}: {str(e)}"
        )

    logger.success(f"reviewed render queued: {task_id}, {len(materials)} materials")
    return utils.get_response(200, {"task_id": task_id})


def _split_saved_materials(
    task_id: str, materials: list, request_id: str
) -> tuple[list, int]:
    """
    Sort a saved clip list into the clips still on disk and a count of the ones
    that are not, rejecting anything resolving outside the allowed directories.

    ``_resolve_reviewed_materials`` treats a missing file as an invalid path,
    which is right when a render is about to start but wrong here: the download
    cache is user-clearable, and a project saved weeks ago naming a since-
    deleted clip should reopen with whatever survived rather than refusing
    outright. Containment is still enforced - the second pass only decides
    whether to report "gone" or "not yours".
    """
    allowed_dirs = [utils.task_dir(task_id), *_material_root_dirs()]
    present: list = []
    missing = 0

    for material_path in materials:
        resolved = None
        for base_dir in allowed_dirs:
            try:
                resolved = file_security.resolve_path_within_directory(
                    base_dir, material_path
                )
                break
            except ValueError:
                continue
        if resolved is not None:
            present.append(resolved)
            continue

        # Nothing matched with the file-must-exist check. Ask again without it:
        # inside an allowed directory means the file was deleted, outside means
        # a path this task was never entitled to read.
        inside_allowed = False
        for base_dir in allowed_dirs:
            try:
                file_security.resolve_path_within_directory(
                    base_dir, material_path, require_file=False
                )
                inside_allowed = True
                break
            except ValueError:
                continue

        if not inside_allowed:
            logger.warning(
                f"reject saved material outside the allowed directories, "
                f"request_id: {request_id}, path: {material_path}"
            )
            raise HttpException(
                task_id=task_id,
                status_code=403,
                message=f"{request_id}: invalid material path",
            )
        missing += 1

    return present, missing


def _task_uses_narration(task_id: str, task: dict) -> bool:
    """
    Whether this task's video has a spoken narration at all.

    While the record is in memory it says so directly: a silent render stores
    an empty ``audio_file``. After a restart the record is rehydrated from
    disk without that field, so the script manifest answers instead - it is
    written before the render and keeps the parameters it ran with. A task
    older than the switch has neither, and was narrated.
    """
    if "audio_file" in task:
        return bool(task.get("audio_file"))

    manifest = task_artifacts.read_script_data(task_id) or {}
    recorded = manifest.get("params")
    if isinstance(recorded, dict):
        return recorded.get("voice_enabled") is not False
    return True


@router.post(
    "/tasks/{task_id}/reopen",
    summary="Reopen a finished task's clips for another review pass",
)
def reopen_task_for_review(
    request: Request,
    body: ReopenTaskRequest,
    task_id: str = Path(..., description="Task ID"),
):
    """
    Hand back a saved clip list in the shape the review screen consumes.

    Re-editing a saved project should show the footage that render actually
    used, not a fresh batch matched against the script again. The client stores
    the clip paths with the project and posts them here; this resolves them the
    same way the render endpoint does, and drops the ones no longer on disk.

    An empty list is not an empty answer: it falls back to the task's own
    record and then to the clips in its directory, which is what makes older
    projects and restarted sessions reopen on their footage too.

    ``renderable`` reports whether any clip survived. ``needs_narration``
    reports whether the render will have to record the voice again - false
    both when the narration is still on disk and when the video never had
    one.

    Read-only: nothing here changes the task. A POST only because the clip list
    is a body, not a query string.
    """
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if not task:
        raise HttpException(
            task_id=task_id, status_code=404, message=f"{request_id}: task not found"
        )

    saved_materials = list(body.materials or [])
    if not saved_materials:
        # Two ways to arrive here with nothing: a project saved before clip
        # lists were stored, and a task rehydrated from disk after a restart,
        # which restores the render but not what it was cut from. The source
        # clips are still in the task directory, so fall back to those - an
        # arbitrary order the user can fix in the review screen beats telling
        # them the footage is gone when it is sitting right there.
        saved_materials = list(task.get("materials") or [])
    if not saved_materials:
        saved_materials = sorted(
            glob.glob(os.path.join(utils.task_dir(task_id), "vid-*.mp4"))
        )

    # A clip can be gone without the task being gone. Dropping it here, with a
    # count, beats failing the render minutes later on a file the user deleted.
    present, missing = _split_saved_materials(task_id, saved_materials, request_id)
    if missing:
        logger.info(
            f"reopen task {task_id}: {missing} of "
            f"{len(present) + missing} saved clips are gone"
        )

    audio_file = task.get("audio_file") or os.path.join(
        utils.task_dir(task_id), "audio.mp3"
    )
    # A video made without narration never had one, and a narration that was
    # cleaned up is recorded again by the render. Neither stops a re-edit.
    uses_narration = _task_uses_narration(task_id, task)
    endpoint = config.app.get("endpoint", "").rstrip("/")

    return utils.get_response(
        200,
        {
            "task_id": task_id,
            "material_paths": present,
            "materials": _streamable_materials(present, endpoint, utils.task_dir()),
            "missing": missing,
            # Clips are the one thing a re-render cannot do without. It used to
            # demand a narration too, which sent every silent project to a
            # "your clips are gone" warning while all of them sat in the task
            # directory - and was wrong for a narrated one as well, since a
            # missing voice track is now recorded again rather than refused.
            "renderable": bool(present),
            # True when rendering will have to record the voice again: worth
            # saying, because it costs a TTS call and follows whatever voice
            # the form has now rather than the one this video was made with.
            "needs_narration": uses_narration and not os.path.exists(audio_file),
            "script": task.get("script") or "",
        },
    )


def create_task(
    request: Request,
    body: Union[TaskVideoRequest, SubtitleRequest, AudioRequest],
    stop_at: str,
):
    task_id = utils.get_uuid()
    request_id = base.get_task_id(request)
    try:
        task = {
            "task_id": task_id,
            "request_id": request_id,
            "params": body.model_dump(),
        }
        sm.state.update_task(task_id)
        try:
            task_manager.add_task(
                tm.start, task_id=task_id, params=body, stop_at=stop_at
            )
        except Exception:
            # 状态记录在调度前创建，默认标记为 processing。如果调度器没能
            # 接管任务（例如线程启动失败或 Redis 队列不可用），必须回滚该
            # 记录，否则 API 和 WebUI 会永久展示一个实际从未运行的任务。
            sm.state.delete_task(task_id)
            raise
        logger.success(f"Task created: {utils.to_json(task)}")
        return utils.get_response(200, task)
    except TaskQueueFullError as e:
        logger.warning(
            f"reject task because queue is full, request_id: {request_id}, task_id: {task_id}"
        )
        raise HttpException(
            task_id=task_id, status_code=429, message=f"{request_id}: {str(e)}"
        )
    except ValueError as e:
        raise HttpException(
            task_id=task_id, status_code=400, message=f"{request_id}: {str(e)}"
        )

def _task_response_data(task: dict, request_id: str) -> dict:
    """
    Shape one task record for the API: strip internals, and turn on-disk paths
    into URIs the client can actually fetch.

    Both the list and the single-task endpoint go through here. They used to
    differ - only the single one converted paths - so the Library received raw
    Windows paths and every "Open video" failed with "not allowed to open url".
    One function means the two cannot drift again.
    """
    endpoint = config.app.get("endpoint", "").rstrip("/")
    tasks_dir = utils.task_dir()
    response_task = _public_task_data(task)

    for key in ("videos", "combined_videos"):
        if task.get(key):
            response_task[key] = [
                _task_file_to_uri(path, endpoint, tasks_dir, request_id)
                for path in task[key]
            ]

    # The narration is an output like the videos are, and the client plays it
    # back the same way: to hear the voice, and to see how long the video will
    # run, before paying for a render. The on-disk path stays out of the
    # response for the reason every other output path does - it names a
    # directory on someone else's machine.
    if task.get("audio_file"):
        response_task["audio_file"] = _task_file_to_uri(
            task["audio_file"], endpoint, tasks_dir, request_id
        )

    if task.get("materials"):
        # The review screen plays each clip before the render, so it needs
        # streamable URIs. The on-disk paths stay under `material_paths`:
        # those are what the render endpoint expects back.
        response_task["material_paths"] = list(task["materials"])
        response_task["materials"] = _streamable_materials(
            task["materials"], endpoint, tasks_dir
        )

    return response_task


@router.get("/tasks", response_model=TaskListResponse, summary="Get all tasks")
def get_all_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
):
    request_id = base.get_task_id(request)
    tasks, total = sm.state.get_all_tasks(page, page_size)

    response = {
        "tasks": [_task_response_data(task, request_id) for task in tasks],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    return utils.get_response(200, response)


@router.get(
    "/tasks/{task_id}", response_model=TaskQueryResponse, summary="Query task status"
)
def get_task(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    query: TaskQueryRequest = Depends(),
):
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task:
        return utils.get_response(200, _task_response_data(task, request_id))

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )



@router.delete(
    "/tasks/{task_id}",
    response_model=TaskDeletionResponse,
    summary="Delete a generated short video task",
)
def delete_video(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task:
        if tm.is_task_busy(task):
            logger.warning(
                f"refuse to delete busy task, request_id: {request_id}, "
                f"task_id: {task_id}, state: {task.get('state')}, "
                f"cross_post_state: {task.get('cross_post_state')}"
            )
            raise HttpException(
                task_id=task_id,
                status_code=409,
                message=f"{request_id}: task is still running",
            )

        tasks_dir = utils.task_dir()
        current_task_dir = os.path.join(tasks_dir, task_id)
        if os.path.exists(current_task_dir):
            _remove_task_thumbnails(current_task_dir)
            shutil.rmtree(current_task_dir)

        sm.state.delete_task(task_id)
        logger.success(f"video deleted: {utils.to_json(task)}")
        return utils.get_response(200)

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


@router.get(
    "/musics", response_model=BgmRetrieveResponse, summary="Retrieve local BGM files"
)
def get_bgm_list(request: Request):
    bgm_list = []
    for file in bgm_service.list_bgm_files():
        filename = os.path.basename(file)
        bgm_list.append(
            {
                # What to show. An upload's storage key carries a uniqueness
                # suffix that means nothing to the user; `file` below keeps the
                # real key, which is what selection and playback use.
                "name": bgm_service.display_bgm_name(filename),
                "size": os.path.getsize(file),
                # 只返回文件名，避免把服务器绝对路径暴露给调用方。服务端会
                # 在 storage/bgm 和 resource/songs 两个白名单目录中重新解析。
                "file": filename,
            }
        )
    response = {"files": bgm_list}
    return utils.get_response(200, response)


@router.post(
    "/musics",
    response_model=BgmUploadResponse,
    summary="Upload a background music file",
    description=(
        "Validate an MP3, M4A, AAC, WAV, FLAC, OGG, OPUS, or WMA file up to "
        "30 MB and store it under an immutable UUID filename in storage/bgm."
    ),
    responses={
        400: {"description": "The filename, format, size, or audio stream is invalid"},
        500: {"description": "FFmpeg validation or persistent storage is unavailable"},
    },
)
def upload_bgm_file(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    try:
        safe_filename = bgm_service.save_bgm_upload(file.filename, file.file)
    except bgm_service.BgmUploadError as exc:
        # 上传失败通常可以由用户更换文件后恢复，因此记录 request_id 和明确原因，
        # 但不输出文件内容或绝对路径，避免日志泄露用户数据。
        logger.warning(
            f"background music upload rejected: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        )
    except bgm_service.BgmServiceError as exc:
        # 工具链或存储故障属于服务端问题，不能伪装成用户文件错误。日志保留
        # request_id 和内部原因，HTTP 响应只返回稳定文案，避免暴露服务器路径。
        logger.error(
            f"background music upload failed: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: background music validation is unavailable",
        )

    response = {"file": safe_filename}
    return utils.get_response(200, response)

@router.get(
    "/video_materials", response_model=VideoMaterialRetrieveResponse, summary="Retrieve local video materials"
)
def get_video_materials_list(request: Request):
    allowed_suffixes = tuple(
        extension.removeprefix(".")
        for extension in material_upload_service.SUPPORTED_MATERIAL_EXTENSIONS
    )
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    files = []
    for suffix in allowed_suffixes:
        files.extend(glob.glob(os.path.join(local_videos_dir, f"*.{suffix}")))
    # 文件系统枚举顺序不稳定，直接返回会导致“顺序拼接”在不同机器或不同
    # 时刻表现不一致。这里统一按文件名排序，至少保证服务端返回顺序可预测。
    files.sort(key=lambda file_path: os.path.basename(file_path).lower())
    video_materials_list = []
    for file in files:
        filename = os.path.basename(file)
        video_materials_list.append(
            {
                "name": filename,
                "size": os.path.getsize(file),
                # 与 BGM 一样，只返回文件名；创建任务时再在 local_videos
                # 白名单目录内解析，避免 API 泄露宿主机绝对路径。
                "file": filename,
            }
        )
    response = {"files": video_materials_list}
    return utils.get_response(200, response)


@router.post(
    "/video_materials",
    response_model=VideoMaterialUploadResponse,
    summary="Upload the video material file to the local videos directory",
)
def upload_video_material_file(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    try:
        # Keep accepting browser-supplied client paths, but persist an immutable
        # UUID storage key so repeated names cannot overwrite queued task inputs.
        safe_filename = _sanitize_upload_filename(file.filename, request_id)
        stored_filename = material_upload_service.save_material_upload(
            safe_filename, file.file
        )
    except material_upload_service.MaterialUploadError as exc:
        logger.warning(
            f"local material upload rejected: request_id={request_id}, "
            f"error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        )
    except material_upload_service.MaterialServiceError as exc:
        logger.error(
            f"local material upload failed: request_id={request_id}, "
            f"error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: local material validation is unavailable",
        )

    response = {"file": stored_filename}
    return utils.get_response(200, response)

def _stream_file_range(
    request: Request, resolved_path: str, media_type: str
) -> StreamingResponse:
    """
    Serve a byte range of an already-resolved file.

    Range handling lives here once: players depend on it for seeking, and its
    edge cases (suffix ranges, out-of-bounds, 416) are subtle enough that a
    second copy would drift. The path must already be inside an allowed
    directory - this function does no checking of its own.
    """
    request_id = base.get_task_id(request)
    range_header = request.headers.get("Range")
    file_size = os.path.getsize(resolved_path)
    start, end = _parse_byte_range(range_header, file_size, request_id)
    length = end - start + 1

    def file_iterator(path, offset=0, bytes_to_read=None):
        with open(path, "rb") as f:
            f.seek(offset, os.SEEK_SET)
            remaining = bytes_to_read or file_size
            while remaining > 0:
                chunk_size = min(4096, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                remaining -= len(data)
                yield data

    response = StreamingResponse(
        file_iterator(resolved_path, start, length), media_type=media_type
    )
    response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    response.status_code = 206  # Partial Content

    return response


def _stream_video_from_directory(
    request: Request, base_dir: str, file_path: str
) -> StreamingResponse:
    """
    Serve a byte range of a video that resolves inside ``base_dir``.

    Every caller passes a client-supplied path, so the allowed root is always
    fixed by the route rather than by the request.
    """
    request_id = base.get_task_id(request)
    video_path = _resolve_path_within_directory(base_dir, file_path, request_id)
    return _stream_file_range(request, video_path, "video/mp4")


@router.get("/stream/{file_path:path}")
async def stream_video(request: Request, file_path: str):
    """Serve a task artifact - a rendered video, or a clip awaiting review."""
    return _stream_video_from_directory(request, utils.task_dir(), file_path)


# The <audio> element picks its decoder from the Content-Type, not the file
# name, so a wrong or generic type is silently unplayable. Only the formats the
# BGM upload already accepts are listed; `.wma` has no entry because no
# browser engine decodes it - the render still handles such a file, the
# in-app preview simply cannot.
_AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}


@router.get("/music_preview/{file_name}")
async def preview_background_music(request: Request, file_name: str):
    """
    Serve a background music track so it can be auditioned before a render.

    Resolution goes through the same helper the render uses, so the preview can
    never reach a file the pipeline would refuse: both the bundled library and
    the user's uploads are covered, and nothing else is.
    """
    request_id = base.get_task_id(request)
    try:
        music_path = bgm_service.resolve_bgm_file(file_name)
    except ValueError as exc:
        logger.warning(
            f"reject music preview, request_id: {request_id}, "
            f"file: {file_name}, error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=403,
            message=f"{request_id}: invalid music file",
        ) from exc

    media_type = _AUDIO_MEDIA_TYPES.get(
        os.path.splitext(music_path)[1].lower(), "application/octet-stream"
    )
    return _stream_file_range(request, music_path, media_type)


@router.get("/material_preview/{file_path:path}")
async def preview_local_material(request: Request, file_path: str):
    """
    Serve a source clip - uploaded or downloaded - for preview.

    Kept separate from /stream/ rather than widening it: the two routes mean
    different things, task output versus source material, and each is pinned to
    its own set of directories. This one only ever reaches clips the app itself
    put there for use in a video.
    """
    request_id = base.get_task_id(request)
    for base_dir in _material_root_dirs():
        try:
            file_security.resolve_path_within_directory(base_dir, file_path)
        except ValueError:
            continue
        return _stream_video_from_directory(request, base_dir, file_path)

    logger.warning(
        f"reject material preview outside the allowed directories, "
        f"request_id: {request_id}, path: {file_path}"
    )
    raise HttpException(
        task_id=request_id, status_code=403, message=f"{request_id}: invalid file path"
    )


def _thumbnail_path_for(source_path: str) -> str:
    """
    Where the cached still for one clip lives.

    Keyed on the full resolved path rather than the file name. Downloaded clips
    are content-named and unique, but task outputs are not: every render writes
    `final-1.mp4`, so a name-based key served the first task's frame for every
    card in the library. Both the route that builds thumbnails and the deletes
    that clean them up go through here.

    The path is normalised here rather than at each call site: two callers
    disagreeing about symlinks or separators would silently produce two keys
    for one clip, which reads as a thumbnail that refuses to be deleted.
    """
    digest = hashlib.sha256(
        os.path.realpath(source_path).encode("utf-8")
    ).hexdigest()
    return os.path.join(utils.storage_dir("thumbnails", create=True), f"{digest}.jpg")


def _remove_task_thumbnails(task_directory: str) -> None:
    """
    Drop the cached stills for everything in a task directory.

    Thumbnails live in storage/thumbnails, so removing the task tree leaves
    them behind forever. Cleaning them is only safe now that the key is the
    file path: under the old name-based key, deleting one task's `final-1.jpg`
    blanked the card of every other task too.
    """
    try:
        names = os.listdir(task_directory)
    except OSError:
        return
    for name in names:
        thumbnail_path = _thumbnail_path_for(os.path.join(task_directory, name))
        try:
            if os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)
        except OSError as exc:
            # A leftover thumbnail is clutter; failing the delete over one
            # would leave the user with a task they cannot remove at all.
            logger.warning(f"could not remove thumbnail {thumbnail_path}: {exc}")


def _build_material_thumbnail(source_path: str, thumbnail_path: str) -> bool:
    """
    Extract one frame as a JPEG, cached next to the other generated data.

    A grid of clips needs to *look* like something before anything is clicked.
    Doing that with <video> elements means a decoder and a partial download per
    tile, which is what made playback stutter earlier; a still image costs a few
    kilobytes and no decoder at all.

    The frame is taken a second in: the opening frame of stock footage is
    routinely a fade from black, which makes every tile look identical.
    """
    # The temporary name has to keep the .jpg suffix: ffmpeg picks its muxer
    # from the extension, and a plain ".tmp" fails with "unable to find a
    # suitable output format" rather than anything about images.
    temporary_path = f"{thumbnail_path}.{os.getpid()}.part.jpg"
    command = [
        utils.get_ffmpeg_binary(),
        "-y",
        "-ss",
        "1",
        "-i",
        source_path,
        "-frames:v",
        "1",
        # image2 treats a single filename as an incomplete sequence pattern.
        # Some builds warn and write anyway, others fail; -update settles it.
        "-update",
        "1",
        "-vf",
        "scale=-2:320",
        "-q:v",
        "4",
        temporary_path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not os.path.exists(temporary_path):
            logger.warning(
                f"thumbnail extraction failed for {os.path.basename(source_path)}: "
                f"{(result.stderr or '').strip()[:200]}"
            )
            return False
        # Rename into place so a second request never serves a half-written
        # file: two tiles asking for the same thumbnail at once is the norm,
        # not an edge case.
        os.replace(temporary_path, thumbnail_path)
        return True
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


@router.get(
    "/material_thumbnail/{file_path:path}",
    summary="Still frame from a source clip, generated on demand",
)
def get_material_thumbnail(
    request: Request,
    file_path: str = Path(..., description="Material file name or tasks-relative path"),
):
    request_id = base.get_task_id(request)
    source_path = None
    # Clips awaiting review live under the task directory, uploads and cached
    # downloads in the material folders. All three are previewable, so all
    # three can be thumbnailed - and nothing outside them can.
    for base_dir in [utils.task_dir(), *_material_root_dirs()]:
        try:
            source_path = file_security.resolve_path_within_directory(
                base_dir, file_path
            )
            break
        except ValueError:
            continue
    if source_path is None:
        logger.warning(
            f"reject thumbnail outside the allowed directories, "
            f"request_id: {request_id}, path: {file_path}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=403,
            message=f"{request_id}: invalid file path",
        )

    thumbnail_path = _thumbnail_path_for(source_path)

    # Re-rendering a reviewed task overwrites its video in place, under the
    # same path and so the same key. A thumbnail older than its source is
    # therefore stale. Comparing two file timestamps is safe where comparing
    # one against `time.time()` is not: both come from the same conversion,
    # and the thumbnail is always written after the clip it was cut from.
    stale = os.path.exists(thumbnail_path) and (
        os.path.getmtime(thumbnail_path) < os.path.getmtime(source_path)
    )
    if stale or not os.path.exists(thumbnail_path):
        if not _build_material_thumbnail(source_path, thumbnail_path):
            raise HttpException(
                task_id=request_id,
                status_code=422,
                message=f"{request_id}: could not read a frame from this clip",
            )

    return FileResponse(
        thumbnail_path,
        media_type="image/jpeg",
        # No long max-age: a task's output keeps its URL across a re-render, so
        # a cached copy can go stale, and the browser has no way to know. These
        # are a few kilobytes over loopback - correctness is worth more here
        # than saving the request.
        headers={"Cache-Control": "no-cache"},
    )


@router.get(
    "/downloaded_materials",
    response_model=DownloadedMaterialRetrieveResponse,
    summary="List stock clips downloaded into the shared cache",
)
def list_downloaded_materials(request: Request):
    """
    List the downloaded clip cache, newest first, with its total size.

    The cache is not bounded: it reached a gigabyte across a handful of renders
    here. Surfacing size and age is what makes it possible to clear out.
    """
    cache_dir = utils.storage_dir("cache_videos", create=True)
    entries = []
    total_size = 0
    for suffix in ("mp4", "mov", "mkv", "webm", "avi"):
        for file_path in glob.glob(os.path.join(cache_dir, f"*.{suffix}")):
            size = os.path.getsize(file_path)
            total_size += size
            entries.append(
                {
                    "name": os.path.basename(file_path),
                    "size": size,
                    # Only the file name, like the other listings: the API must
                    # not hand out absolute host paths.
                    "file": os.path.basename(file_path),
                    "modified": os.path.getmtime(file_path),
                }
            )
    entries.sort(key=lambda entry: entry["modified"], reverse=True)
    return utils.get_response(200, {"files": entries, "total_size": total_size})


@router.delete(
    "/downloaded_materials/{file_name}",
    response_model=TaskDeletionResponse,
    summary="Delete one downloaded clip from disk",
)
def delete_downloaded_material(
    request: Request, file_name: str = Path(..., description="Cached file name")
):
    request_id = base.get_task_id(request)
    cache_dir = utils.storage_dir("cache_videos", create=True)
    file_path = _resolve_path_within_directory(cache_dir, file_name, request_id)
    os.remove(file_path)

    # The thumbnail is derived from a file that no longer exists, so it can
    # only ever be dead weight from here on.
    thumbnail_path = _thumbnail_path_for(file_path)
    if os.path.exists(thumbnail_path):
        os.remove(thumbnail_path)

    logger.success(f"downloaded material deleted: {os.path.basename(file_path)}")
    return utils.get_response(200)


@router.get("/download/{file_path:path}")
async def download_video(request: Request, file_path: str):
    """
    download video
    :param request: Request request
    :param file_path: video file path, eg: /cd1727ed-3473-42a2-a7da-4faafafec72b/final-1.mp4
    :return: video file
    """
    request_id = base.get_task_id(request)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    file_path = pathlib.Path(video_path)
    filename = file_path.stem
    extension = file_path.suffix
    headers = {"Content-Disposition": f"attachment; filename={filename}{extension}"}
    return FileResponse(
        path=video_path,
        headers=headers,
        filename=f"{filename}{extension}",
        media_type=f"video/{extension[1:]}",
    )
