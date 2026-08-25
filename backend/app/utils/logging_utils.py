import os
import threading

from loguru import logger


PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
)
# ``{exception}`` has to be here explicitly. Loguru appends it for a format
# given as a string, but not for a format *function* - and this one is a
# function, so for as long as it was missing, every logger.exception() and
# every exc_info=True in the backend wrote its message and dropped the
# traceback. utils.run_in_background logs task failures that way, so a render
# that died in a worker thread left one line in logs/backend.log and no stack
# at all, which is the opposite of why that file exists.
LOG_RECORD_FORMAT = (
    "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
    "<level>{level}</> | "
    '"{file.path}:{line}":<blue> {function}</> '
    "- <level>{message}</>\n{exception}"
)
# Loguru 启动时默认终端 handler 的 ID 为 0。WebUI 重新加载时只能替换这个
# 基础终端输出，不能调用 logger.remove() 清空全部 handler，否则正在运行任务
# 用于收集 WebUI 日志的临时 sink 也会被删除。
_terminal_handler_id: int | None = 0
_terminal_handler_lock = threading.RLock()


def format_log_record(record):
    """
    统一格式化终端与 WebUI 日志。

    Loguru 会把同一条记录交给多个 sink。第一个 sink 可能已经将绝对路径转换
    为项目相对路径，因此这里同时兼容绝对路径和 ``./`` 开头的已格式化路径。
    WebUI sink 会关闭颜色，但时间、级别、调用位置和消息内容与终端保持一致。
    """
    file_path = record["file"].path
    if os.path.isabs(file_path):
        relative_path = os.path.relpath(file_path, PROJECT_ROOT)
        record["file"].path = f"./{relative_path}"

    # 日志消息有时会包含任务文件的绝对路径。统一缩短为项目相对路径，可以
    # 避免 WebUI 和终端因初始化入口不同而展示两套内容。
    record["message"] = record["message"].replace(PROJECT_ROOT, ".")
    return LOG_RECORD_FORMAT


def configure_terminal_logger(sink, level: str, colorize: bool = True) -> int:
    """
    安全替换进程级终端日志 handler，并保留任务专用 handler。

    Streamlit 在代码热重载或缓存失效时可能重新执行日志初始化。这里只按已记录
    的 handler ID 精确移除旧终端输出，因此不会中断后台任务正在写入的 WebUI
    日志。锁用于保护多个浏览器会话同时初始化时的 ID 更新。
    """
    global _terminal_handler_id

    with _terminal_handler_lock:
        if _terminal_handler_id is not None:
            try:
                logger.remove(_terminal_handler_id)
            except ValueError:
                # 测试或外部入口可能已经移除该 handler。继续创建新的终端输出，
                # 不需要影响其它仍有效的日志 sink。
                pass

        _terminal_handler_id = logger.add(
            sink,
            level=level,
            format=format_log_record,
            colorize=colorize,
            # Loguru 默认 diagnose=True，会把每个栈帧的局部变量值渲染进
            # traceback。桌面版把 stdout/stderr 重定向到 logs/backend.log，
            # 而 API Key 在 LLM、TTS、素材请求中就是局部变量——一次异常足以
            # 让凭据落进用户排查问题时会发给别人的那个文件。
            # backtrace 一并关闭：它展开的调用链同样包含上游帧的变量值。
            backtrace=False,
            diagnose=False,
        )
        return _terminal_handler_id
