"""日志 sink 不得把局部变量值渲染进 traceback。

Loguru 的 ``diagnose`` 默认为 True，会在异常回溯里逐帧打印局部变量的值。桌面版
把后端的 stdout/stderr 重定向到 logs/backend.log，而 API Key 在 LLM 请求、TTS
合成、素材下载里就是普通局部变量——一次异常就足以把凭据写进那个"出问题时发给
别人"的文件。

这里断言的是行为而不是参数：即使将来换掉日志库或改写 sink 配置，只要凭据重新
出现在输出里，用例就会失败。
"""

import io
import sys
import unittest

from loguru import logger

from app.utils import logging_utils, utils


class TestTerminalLoggerDoesNotLeakLocals(unittest.TestCase):
    SECRET = "sk-should-never-be-logged"

    def setUp(self):
        self.sink = io.StringIO()
        logging_utils.configure_terminal_logger(
            self.sink, level="DEBUG", colorize=False
        )
        # sink 是进程级资源，用例结束后必须把终端输出恢复回去，否则后续用例
        # 的日志会继续写进这个已经没人读的缓冲区。
        self.addCleanup(
            logging_utils.configure_terminal_logger,
            sys.stdout,
            "INFO",
            True,
        )

    def _raise_with_a_credential_in_scope(self):
        # 刻意复现真实调用点的形状：密钥以普通局部变量的身份出现在失败那一行
        # 的表达式里。loguru 的 diagnose 正是按这种名字来渲染取值的。
        api_key = self.SECRET
        headers = {"authorization": "Bearer " + api_key}
        raise RuntimeError("provider rejected the request: %s" % len(headers))

    def test_exception_backtraces_do_not_render_local_variables(self):
        try:
            self._raise_with_a_credential_in_scope()
        except RuntimeError:
            logger.exception("failed to reach the provider")

        output = self.sink.getvalue()

        self.assertIn("failed to reach the provider", output)
        self.assertNotIn(self.SECRET, output)

    def test_a_failed_background_task_logs_a_stack_but_no_credentials(self):
        """
        工作线程崩溃时，logs/backend.log 是唯一的现场记录。

        两件事必须同时成立：栈要写进去，否则安装后的构建挂掉时无据可查；
        局部变量的值不能写进去，否则同一份文件会带着凭据被发出去。
        """

        utils.run_in_background(self._raise_with_a_credential_in_scope).join()

        output = self.sink.getvalue()

        self.assertIn("run_in_background error", output)
        self.assertIn("Traceback", output)
        self.assertNotIn(self.SECRET, output)


if __name__ == "__main__":
    unittest.main()
