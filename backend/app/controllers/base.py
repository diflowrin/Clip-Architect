import secrets
from typing import Annotated
from uuid import uuid4

from fastapi import Header, Request

from app.config import config
from app.models.exception import HttpException

MAX_TASK_ID_LENGTH = 128


def normalize_task_id(value: object) -> str:
    """Return a log-safe request ID, replacing invalid client input with a UUID."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TASK_ID_LENGTH
        or not value.isprintable()
    ):
        return str(uuid4())
    return value


def get_task_id(request: Request) -> str:
    return normalize_task_id(request.headers.get("x-task-id"))


def get_api_key(request: Request):
    api_key = request.headers.get("x-api-key")
    return api_key


def get_api_key_values(request: Request) -> list[str]:
    """返回请求中全部 API Key Header，保留重复值用于安全校验。"""

    # Starlette Headers 提供 getlist()，可以区分代理或客户端发送的重复 Header。
    # 单元测试中的轻量 Request 替身只使用普通 dict，因此保留兼容回退。
    get_list = getattr(request.headers, "getlist", None)
    if callable(get_list):
        return [value for value in get_list("x-api-key") if isinstance(value, str)]

    api_key = get_api_key(request)
    return [api_key] if isinstance(api_key, str) else []


API_KEY_QUERY_PARAM = "api_key"

# The methods that may carry the key in the query string instead of a header.
#
# The UI reaches several routes as subresources - a <video src> for a render or
# a clip, an <img src>/poster for a thumbnail, an @font-face for the subtitle
# preview - and none of those can be given a request header. A header-only rule
# would authenticate the API correctly and leave every one of them showing a
# broken tile.
#
# Confined to the safe methods on purpose: a URL leaks far more readily than a
# header does, into logs, browser history and the shell's "open in the player"
# handoff, so nothing that changes state or spends money may be reachable from
# one.
_QUERY_KEY_METHODS = frozenset({"GET", "HEAD"})


def get_query_api_key_values(request: Request) -> list[str]:
    """返回 GET/HEAD 请求查询串中的全部 API Key，重复值同样保留。"""

    if str(getattr(request, "method", "")).upper() not in _QUERY_KEY_METHODS:
        return []

    # 与 Header 相同的兼容处理：测试替身可能不带 query_params。
    query_params = getattr(request, "query_params", None)
    get_list = getattr(query_params, "getlist", None)
    if not callable(get_list):
        return []

    return [
        value
        for value in get_list(API_KEY_QUERY_PARAM)
        if isinstance(value, str)
    ]


def configured_api_key():
    """The key requests have to present, or ``""`` when the API is open.

    Two sources, because the same backend serves two very different
    deployments. The desktop shell generates a key per launch and passes it in
    the environment, which is what the frontend was handed and therefore what
    it will send; a source checkout or a Docker run keeps configuring one in
    config.toml, or none at all. The session key wins when both exist.

    Returns the raw configured value rather than a string: ``verify_token``
    rejects a non-string type explicitly instead of letting it be coerced.
    """
    session_key = getattr(config, "session_api_key", "")
    if session_key:
        return session_key
    return config.app.get("api_key", "")


def verify_token(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
):
    """按配置决定是否校验 API Key。

    空 Key 保留现有的本地免认证模式；配置非空 Key 后，API 路由和任务产物
    下载都会要求客户端通过 ``x-api-key`` 请求头提供同一个值。桌面外壳每次
    启动注入的一次性 Key 也走这里，见 ``configured_api_key``。参数声明同时
    让 Swagger 展示该请求头，便于受保护环境调试。

    浏览器无法为 ``<video src>`` 这类子资源设置请求头，因此 GET/HEAD 允许
    改用查询串传递同一个 Key，见 ``get_query_api_key_values``。
    """

    configured_key = configured_api_key()
    if configured_key in (None, ""):
        return None

    # 配置项必须是字符串。这里拒绝列表、数字等错误类型，避免字符串隐式
    # 转换产生难以发现的认证行为；错误信息也不包含实际 Key。
    if not isinstance(configured_key, str):
        raise HttpException(
            task_id=get_task_id(request),
            status_code=500,
            message="API authentication is misconfigured",
        )

    # FastAPI 参数用于在 OpenAPI 中声明 x-api-key；实际校验始终读取 Request，
    # 才能识别同名 Header 被重复发送的情况。普通客户端和反向代理对重复 Header
    # 的取值顺序可能不同，因此必须拒绝，而不能隐式采用第一个或最后一个值。
    token_values = get_api_key_values(request)
    if not token_values and isinstance(x_api_key, str):
        token_values = [x_api_key]
    # 只有在完全没有 Header 时才回落到查询串。同时出现两种来源意味着客户端
    # 意图不明确，交给下面的数量检查按重复凭据拒绝，而不是猜一个。
    if not token_values:
        token_values = get_query_api_key_values(request)

    if len(token_values) != 1:
        raise HttpException(
            task_id=get_task_id(request),
            status_code=401,
            message="invalid API key",
        )

    # compare_digest 对 str 只支持 ASCII。请求 Header 属于不可信输入，攻击者
    # 可以发送 Latin-1 字符触发 TypeError。统一编码为 UTF-8 bytes 后既保留
    # 恒定时间比较，也支持 TOML 中合法的 Unicode Key。
    token = token_values[0]
    if not secrets.compare_digest(
        token.encode("utf-8"), configured_key.encode("utf-8")
    ):
        raise HttpException(
            task_id=get_task_id(request),
            status_code=401,
            message="invalid API key",
        )

    return None
