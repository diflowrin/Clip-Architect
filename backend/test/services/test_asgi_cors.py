"""CORS 策略：默认封闭，且通配符永远不与凭据同时生效。

之前的默认值是 ``["*"]``，Starlette 会把它与 ``allow_credentials=True`` 组合成
"回显请求方的 Origin"，于是用户访问的任意网站都能读取本 API 的响应，其中包括
/api/v1/settings 里的全部 Provider 密钥。桌面外壳总会显式传入允许列表，因此
这个默认值只在有人手工启动后端时生效——而那恰恰是配置里存着真实密钥的场景。
"""

import unittest

from fastapi.testclient import TestClient

from app import asgi
from app.config import config


class TestCORSPolicy(unittest.TestCase):
    def test_an_empty_configuration_falls_back_to_the_webview_origins(self):
        origins, allow_credentials = asgi.cors_policy("")

        self.assertEqual(origins, list(asgi._DEFAULT_ALLOWED_ORIGINS))
        self.assertNotIn("*", origins)
        self.assertTrue(allow_credentials)

    def test_a_wildcard_disables_credentials(self):
        """通配符仍然可用，但不能再让任意来源读取带凭据的响应。"""

        origins, allow_credentials = asgi.cors_policy("*")

        self.assertEqual(origins, ["*"])
        self.assertFalse(allow_credentials)

    def test_a_wildcard_mixed_into_a_list_also_disables_credentials(self):
        """写成列表的一部分同样是"允许任意来源"，不能因为形式不同就放行。"""

        _, allow_credentials = asgi.cors_policy("tauri://localhost,*")

        self.assertFalse(allow_credentials)

    def test_configured_origins_are_trimmed_and_keep_credentials(self):
        origins, allow_credentials = asgi.cors_policy(
            " https://one.example , https://two.example "
        )

        self.assertEqual(origins, ["https://one.example", "https://two.example"])
        self.assertTrue(allow_credentials)


class TestCORSResponses(unittest.TestCase):
    """通过真实 ASGI 入口验证当前进程实际生效的策略。"""

    def setUp(self):
        self.original_app_config = dict(config.app)
        config.app["api_key"] = ""
        self.client = TestClient(asgi.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def test_the_webview_origin_may_read_responses(self):
        response = self.client.get(
            "/api/v1/tasks", headers={"Origin": "tauri://localhost"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"), "tauri://localhost"
        )

    def test_an_unlisted_origin_gets_no_allow_origin_header(self):
        """没有这个响应头，浏览器就不会把响应体交给发起请求的页面。"""

        response = self.client.get(
            "/api/v1/tasks", headers={"Origin": "https://evil.example"}
        )

        self.assertIsNone(response.headers.get("access-control-allow-origin"))

    def test_the_vite_dev_origin_is_not_allowed_by_default(self):
        """1420 是 Vite 的默认端口，只应由外壳在 debug 构建中显式加入。"""

        response = self.client.get(
            "/api/v1/tasks", headers={"Origin": "http://localhost:1420"}
        )

        self.assertIsNone(response.headers.get("access-control-allow-origin"))


if __name__ == "__main__":
    unittest.main()
