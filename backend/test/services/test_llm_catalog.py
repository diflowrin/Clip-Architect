import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import llm_catalog


def _response(payload):
    """A requests.Response stand-in that only does what the fetch touches."""
    return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)


class TestLlmCatalog(unittest.TestCase):
    """
    模型目录是给下拉框用的辅助数据，任何失败都必须降级为“手填模型 ID”，
    而不是把用户挡在设置页外面。
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        llm_catalog._cache.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        llm_catalog._cache.clear()

    def test_reads_an_openai_shaped_listing(self):
        """OpenRouter 和大多数网关都返回 {"data": [{"id": ...}]}。"""
        payload = {
            "data": [
                {"id": "openai/gpt-5.5", "name": "OpenAI: GPT-5.5"},
                {"id": "anthropic/claude-sonnet-5"},
            ]
        }

        with patch.object(llm_catalog.requests, "get", return_value=_response(payload)):
            result = llm_catalog.list_models("openrouter")

        self.assertIsNone(result["error"])
        self.assertEqual(
            result["models"],
            [
                # Sorted by id, so the dropdown order does not depend on
                # whatever order the provider happened to answer in.
                {
                    "id": "anthropic/claude-sonnet-5",
                    "label": "anthropic/claude-sonnet-5",
                    # No pricing published is unknown, never free: the "only
                    # models that cost nothing" filter has to be safe to trust.
                    "free": False,
                },
                {"id": "openai/gpt-5.5", "label": "OpenAI: GPT-5.5", "free": False},
            ],
        )

    def test_reads_free_from_pricing_rather_than_the_id(self):
        """
        A model is free when the provider prices it at zero.

        Not when its id ends in ``:free``: on OpenRouter more entries are
        priced at zero than carry the suffix, so the name is the less complete
        answer and would hide models from the filter that costs nothing.
        """
        payload = {
            "data": [
                {
                    "id": "nvidia/nemotron-3-nano-30b-a3b:free",
                    "pricing": {"prompt": "0", "completion": "0"},
                },
                # Zero-priced with no suffix: the case the id test misses.
                {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}},
                # Free prompt, billed completion. Not free.
                {"id": "vendor/half", "pricing": {"prompt": "0", "completion": "0.002"}},
                # Suffix claims free, pricing disagrees. Pricing wins.
                {"id": "vendor/liar:free", "pricing": {"prompt": "0.01", "completion": "0"}},
                {"id": "vendor/unpriced"},
            ]
        }

        with patch.object(llm_catalog.requests, "get", return_value=_response(payload)):
            result = llm_catalog.list_models("openrouter")

        free = {model["id"] for model in result["models"] if model["free"]}
        self.assertEqual(
            free, {"nvidia/nemotron-3-nano-30b-a3b:free", "openrouter/free"}
        )

    def test_skips_entries_without_a_usable_id(self):
        """
        目录里少一个条目只是少一个选项；因为一条脏数据整份列表变空，
        才会让用户以为供应商没有模型。
        """
        payload = {
            "data": [
                {"name": "no id at all"},
                {"id": "   "},
                "not an object",
                {"id": "openai/gpt-5.5"},
                {"id": "openai/gpt-5.5", "name": "duplicate"},
            ]
        }

        with patch.object(llm_catalog.requests, "get", return_value=_response(payload)):
            result = llm_catalog.list_models("openrouter")

        self.assertEqual([model["id"] for model in result["models"]], ["openai/gpt-5.5"])

    def test_a_response_that_is_not_a_listing_reads_as_empty(self):
        """网关返回错误页或异常结构时，只当作“没有目录”，不能抛异常。"""
        for payload in ({"error": "nope"}, [], "text", None):
            with self.subTest(payload=payload):
                llm_catalog._cache.clear()
                with patch.object(
                    llm_catalog.requests, "get", return_value=_response(payload)
                ):
                    result = llm_catalog.list_models("openrouter")
                self.assertEqual(result["models"], [])

    def test_a_successful_fetch_is_cached(self):
        """设置页每次打开都拉一遍几百个模型，既慢又毫无必要。"""
        payload = {"data": [{"id": "openai/gpt-5.5"}]}

        with patch.object(
            llm_catalog.requests, "get", return_value=_response(payload)
        ) as fetch:
            first = llm_catalog.list_models("openrouter")
            second = llm_catalog.list_models("openrouter")

        fetch.assert_called_once()
        self.assertEqual(first["models"], second["models"])

    def test_a_failure_is_reported_and_not_cached(self):
        """
        网络抖动不能被记住半小时。失败只报原因，下一次调用照常重试。
        """
        payload = {"data": [{"id": "openai/gpt-5.5"}]}

        with patch.object(
            llm_catalog.requests, "get", side_effect=OSError("connection refused")
        ):
            failed = llm_catalog.list_models("openrouter")

        self.assertEqual(failed["models"], [])
        self.assertIn("connection refused", failed["error"])

        with patch.object(llm_catalog.requests, "get", return_value=_response(payload)):
            recovered = llm_catalog.list_models("openrouter")

        self.assertIsNone(recovered["error"])
        self.assertEqual(len(recovered["models"]), 1)

    def test_an_expired_list_survives_a_later_failure(self):
        """
        半小时前真实存在的列表，比一个空下拉框有用得多。过期条目继续展示，
        同时把错误一起返回，让界面可以说明它可能已经过时。
        """
        payload = {"data": [{"id": "openai/gpt-5.5"}]}

        with patch.object(llm_catalog.requests, "get", return_value=_response(payload)):
            llm_catalog.list_models("openrouter")

        for entry in llm_catalog._cache.values():
            entry["fetched_at"] = time.time() - llm_catalog._CACHE_TTL_SECONDS - 1

        with patch.object(llm_catalog.requests, "get", side_effect=OSError("offline")):
            stale = llm_catalog.list_models("openrouter")

        self.assertEqual([model["id"] for model in stale["models"]], ["openai/gpt-5.5"])
        self.assertIn("offline", stale["error"])

    def test_the_catalogue_follows_the_configured_base_url(self):
        """
        指向网关的用户想看到那个网关真正会路由的模型，而不是上游的完整目录。
        """
        config.app["openrouter_base_url"] = "https://gateway.example/v1/"
        fetch = MagicMock(return_value=_response({"data": []}))

        with patch.object(llm_catalog.requests, "get", fetch):
            llm_catalog.list_models("openrouter")

        self.assertEqual(fetch.call_args.args[0], "https://gateway.example/v1/models")

    def test_the_api_key_travels_when_one_is_configured(self):
        """OpenRouter 的目录免鉴权，但私有网关通常不是。"""
        config.app["openrouter_api_key"] = "sk-test"
        fetch = MagicMock(return_value=_response({"data": []}))

        with patch.object(llm_catalog.requests, "get", fetch):
            llm_catalog.list_models("openrouter")

        self.assertEqual(
            fetch.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test"
        )

    def test_no_key_means_no_authorization_header(self):
        """
        没有 Key 也要能看到列表：否则用户必须先盲填一个模型名才能挑模型。
        """
        config.app["openrouter_api_key"] = ""
        fetch = MagicMock(return_value=_response({"data": []}))

        with patch.object(llm_catalog.requests, "get", fetch):
            llm_catalog.list_models("openrouter")

        self.assertNotIn("Authorization", fetch.call_args.kwargs["headers"])

    def test_a_provider_without_a_catalogue_never_reaches_the_network(self):
        """没有声明目录路径的 Provider 请求任何地址都是错的。"""
        fetch = MagicMock()

        with patch.object(llm_catalog.requests, "get", fetch):
            result = llm_catalog.list_models("openai")

        fetch.assert_not_called()
        self.assertEqual(result, {"models": [], "error": None})

    def test_an_unknown_provider_is_reported_rather_than_fetched(self):
        fetch = MagicMock()

        with patch.object(llm_catalog.requests, "get", fetch):
            result = llm_catalog.list_models("not-a-provider")

        fetch.assert_not_called()
        self.assertEqual(result["models"], [])
        self.assertIn("unsupported llm provider", result["error"])

    def test_the_request_honours_the_proxy_and_tls_settings(self):
        """
        为企业代理关掉证书校验的用户，不能因为模型目录是唯一漏掉的请求而失败。
        """
        config.app["tls_verify"] = "false"
        fetch = MagicMock(return_value=_response({"data": []}))

        with patch.object(llm_catalog.requests, "get", fetch):
            llm_catalog.list_models("openrouter")

        self.assertIs(fetch.call_args.kwargs["verify"], False)
        self.assertEqual(fetch.call_args.kwargs["proxies"], config.proxy)


if __name__ == "__main__":
    unittest.main()
