import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.utils import utils


class TestAPIAuthenticationHTTP(unittest.TestCase):
    """从真实 ASGI 入口验证 V1 API 的可选鉴权，覆盖两个业务路由组。"""

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.client = TestClient(asgi.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_empty_key_preserves_existing_open_access(self):
        """默认空 Key 不要求请求头，保证旧客户端和本地 WebUI 继续工作。"""

        config.app["api_key"] = ""

        response = self.client.get("/api/v1/tasks")

        self.assertEqual(response.status_code, 200)

    def test_video_routes_require_matching_key_when_configured(self):
        """视频路由在启用保护后必须统一拒绝缺失和错误的 Key。"""

        config.app["api_key"] = "video-secret"

        missing = self.client.get("/api/v1/tasks")
        wrong = self.client.get(
            "/api/v1/tasks",
            headers={"x-api-key": "wrong"},
        )
        accepted = self.client.get(
            "/api/v1/tasks",
            headers={"x-api-key": "video-secret"},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(accepted.status_code, 200)

    def test_llm_routes_authenticate_before_request_validation(self):
        """LLM 路由必须先鉴权，未认证请求不得进入会产生费用的业务逻辑。"""

        config.app["api_key"] = "llm-secret"

        # 请求模型提供了默认值，空请求也可能真实调用大模型。这里隔离外部
        # 服务并核对调用次数，既验证鉴权顺序，也避免测试消耗用户的 API。
        with patch(
            "app.controllers.v1.llm.llm.generate_script",
            return_value="mocked script",
        ) as generate_script:
            missing = self.client.post("/api/v1/scripts", json={})
            accepted = self.client.post(
                "/api/v1/scripts",
                json={},
                headers={"x-api-key": "llm-secret"},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        generate_script.assert_called_once()

    def test_openapi_documents_api_key_header_for_v1_routes(self):
        """Swagger 必须显示 x-api-key，避免启用保护后只能靠猜测请求格式。"""

        schema = self.client.get("/openapi.json").json()
        parameters = schema["paths"]["/api/v1/tasks"]["get"]["parameters"]

        self.assertTrue(
            any(
                parameter["in"] == "header" and parameter["name"] == "x-api-key"
                for parameter in parameters
            )
        )

    def test_duplicate_api_key_headers_are_rejected(self):
        """重复凭据的解释可能因代理不同而变化，因此无论顺序都必须拒绝。"""

        config.app["api_key"] = "video-secret"

        correct_first = self.client.get(
            "/api/v1/tasks",
            headers=[
                ("x-api-key", "video-secret"),
                ("x-api-key", "wrong"),
            ],
        )
        wrong_first = self.client.get(
            "/api/v1/tasks",
            headers=[
                ("x-api-key", "wrong"),
                ("x-api-key", "video-secret"),
            ],
        )

        self.assertEqual(correct_first.status_code, 401)
        self.assertEqual(wrong_first.status_code, 401)


class TestSessionAPIKey(unittest.TestCase):
    """桌面外壳注入的一次性 Key：不落盘、不进设置表单、覆盖全部路由。"""

    SESSION_KEY = "e6f0c2a1b3d4e5f60718293a4b5c6d7e"

    def setUp(self):
        self.original_app_config = dict(config.app)
        # 外壳注入 Key 时 config.toml 里的值仍然是空的，测试必须复现这一点：
        # 如果实现只读取 config.app，下面每个断言都会以“API 开放”通过。
        config.app["api_key"] = ""
        self.client = TestClient(asgi.app)
        session_key = patch.object(config, "session_api_key", self.SESSION_KEY)
        session_key.start()
        self.addCleanup(session_key.stop)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_session_key_protects_the_api_while_config_stays_empty(self):
        missing = self.client.get("/api/v1/tasks")
        wrong = self.client.get("/api/v1/tasks", headers={"x-api-key": "wrong"})
        accepted = self.client.get(
            "/api/v1/tasks", headers={"x-api-key": self.SESSION_KEY}
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        # 一次性 Key 不得渗入可持久化配置，否则 save_config() 会把它写进
        # config.toml，关闭应用后它仍然有效。
        self.assertEqual(config.app.get("api_key"), "")

    def test_session_key_never_reaches_the_settings_form(self):
        """设置界面从 /settings 渲染输入框，Key 出现在那里等于既落盘又显示。"""

        response = self.client.get(
            "/api/v1/settings", headers={"x-api-key": self.SESSION_KEY}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["app"]["api_key"], "")

    def test_get_routes_accept_the_key_in_the_query_string(self):
        """<video src> 无法携带请求头，因此 GET 必须接受查询串传递的同一个 Key。"""

        accepted = self.client.get(f"/api/v1/tasks?api_key={self.SESSION_KEY}")
        wrong = self.client.get("/api/v1/tasks?api_key=wrong")

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(wrong.status_code, 401)

    def test_task_files_accept_the_key_in_the_query_string(self):
        """/tasks 走中间件而不是路由依赖，是与上面不同的一条代码路径。"""

        with tempfile.TemporaryDirectory(
            prefix="session-key-", dir=utils.task_dir()
        ) as task_directory:
            task_path = Path(task_directory)
            artifact = task_path / "artifact.txt"
            artifact.write_text("task artifact", encoding="utf-8")
            url = f"/tasks/{task_path.name}/{artifact.name}"

            missing = self.client.get(url)
            accepted = self.client.get(f"{url}?api_key={self.SESSION_KEY}")

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.text, "task artifact")

    def test_query_string_key_is_refused_on_state_changing_methods(self):
        """URL 比请求头更容易泄漏到日志和历史记录，因此写操作只认请求头。"""

        with patch(
            "app.controllers.v1.llm.llm.generate_script", return_value="mocked script"
        ) as generate_script:
            from_query = self.client.post(
                f"/api/v1/scripts?api_key={self.SESSION_KEY}", json={}
            )
            from_header = self.client.post(
                "/api/v1/scripts", json={}, headers={"x-api-key": self.SESSION_KEY}
            )

        self.assertEqual(from_query.status_code, 401)
        self.assertEqual(from_header.status_code, 200)
        # 鉴权必须发生在业务逻辑之前，被拒绝的请求不得触发一次付费调用。
        generate_script.assert_called_once()

    def test_duplicate_query_string_keys_are_rejected(self):
        """与重复请求头同理：取第一个还是最后一个，各层解析并不一致。"""

        response = self.client.get(
            f"/api/v1/tasks?api_key={self.SESSION_KEY}&api_key=wrong"
        )

        self.assertEqual(response.status_code, 401)

    def test_configured_file_key_still_applies_without_a_session_key(self):
        """源码和 Docker 部署没有外壳，config.toml 中的 Key 必须继续生效。"""

        with patch.object(config, "session_api_key", ""):
            config.app["api_key"] = "file-secret"

            missing = self.client.get("/api/v1/tasks")
            accepted = self.client.get(
                "/api/v1/tasks", headers={"x-api-key": "file-secret"}
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(accepted.status_code, 200)


if __name__ == "__main__":
    unittest.main()
