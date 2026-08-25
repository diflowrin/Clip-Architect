"""Application implementation - ASGI."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import config
from app.controllers import base
from app.models.exception import HttpException
from app.router import root_api_router
from app.utils import utils


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    """集中处理 API 进程启动恢复和关闭日志。"""
    logger.info("startup event")

    # 与 verify_token 读取同一个来源，否则外壳注入一次性 Key 后这行日志会
    # 继续声称鉴权已关闭，把排查引向完全错误的方向。
    configured_api_key = base.configured_api_key()
    if configured_api_key in (None, ""):
        logger.warning(
            "API key authentication is disabled; keep the API on a trusted network"
        )
    elif isinstance(configured_api_key, str):
        # 只记录保护范围，不得输出 Key、长度或摘要，避免凭据进入日志系统。
        logger.info("API key authentication is enabled for /api/v1 and /tasks")
    else:
        logger.error(
            "API key authentication is misconfigured: app.api_key must be a string"
        )

    # 跨平台发布由当前进程线程池执行，不会在服务重启后恢复。启动时把 Redis
    # 中确认已失去执行进程的活动状态收敛为失败，避免任务永久无法删除。
    from app.services import task as task_service

    # 默认状态后端只存在于进程内存中，重启后整个作品库会消失，而视频仍在
    # 磁盘上。以磁盘为准重建已完成任务，让库在关闭应用后依然存在。
    task_service.rehydrate_finished_tasks_from_disk()

    task_service.recover_interrupted_cross_posts()
    try:
        yield
    finally:
        logger.info("shutdown event")


def exception_handler(request: Request, e: HttpException):
    return JSONResponse(
        status_code=e.status_code,
        content=utils.get_response(e.status_code, e.data, e.message),
    )


def validation_exception_handler(request: Request, e: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=utils.get_response(
            status=400, data=e.errors(), message="field required"
        ),
    )


def get_application() -> FastAPI:
    """Initialize FastAPI application.

    Returns:
       FastAPI: Application object instance.

    """
    instance = FastAPI(
        title=config.project_name,
        description=config.project_description,
        version=config.project_version,
        debug=False,
        lifespan=application_lifespan,
    )
    instance.include_router(root_api_router)
    instance.add_exception_handler(HttpException, exception_handler)
    instance.add_exception_handler(RequestValidationError, validation_exception_handler)
    return instance


app = get_application()


@app.middleware("http")
async def protect_generated_task_files(request: Request, call_next):
    """保护任务产物静态路由，防止绕过 API 鉴权直接下载。

    ``/tasks`` 由 StaticFiles 独立挂载，无法复用 APIRouter 的依赖，
    因此在中间件中调用同一个 verify_token。鉴权函数会在未配置
    api_key 时放行；OPTIONS 预检请求也保留给 CORS 中间件处理。
    """

    request_path = request.url.path
    is_task_file = request_path == "/tasks" or request_path.startswith("/tasks/")
    if is_task_file and request.method != "OPTIONS":
        try:
            base.verify_token(request)
        except HttpException as exception:
            return exception_handler(request, exception)

    return await call_next(request)


# Configures the CORS middleware for the FastAPI app.
#
# The default used to be ``["*"]``, which Starlette combines with
# ``allow_credentials=True`` by reflecting whatever Origin it was sent - so any
# website the user visited could read this API's responses, including every
# provider key in /api/v1/settings. The desktop shell always passes an explicit
# list, so that default only ever applied to a backend started by hand, which
# is exactly when the config holds real keys.
#
# The closed default is the set of origins a Tauri webview uses. Anything else
# has to say so through the environment variable.
_DEFAULT_ALLOWED_ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
)

def cors_policy(configured_origins: str) -> tuple[list[str], bool]:
    """Return the origins to allow, and whether credentials may accompany them.

    A wildcard stays available for anyone who deliberately asks for it, but
    never together with credentials: that pairing is what turns "allow any
    origin" into "let any origin read authenticated responses".
    """
    origins = [
        origin.strip()
        for origin in configured_origins.split(",")
        if origin.strip()
    ] or list(_DEFAULT_ALLOWED_ORIGINS)
    return origins, "*" not in origins


origins, allow_credentials = cors_policy(os.getenv("CORS_ALLOWED_ORIGINS", ""))
if not allow_credentials:
    logger.warning(
        "CORS is configured to allow any origin; credentials are disabled for "
        "that configuration"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

task_dir = utils.task_dir()
app.mount("/tasks", StaticFiles(directory=task_dir, html=True), name="")

public_dir = utils.public_dir()
app.mount("/", StaticFiles(directory=public_dir, html=True), name="")
