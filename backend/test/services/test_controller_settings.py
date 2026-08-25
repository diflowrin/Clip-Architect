import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import toml
from fastapi.testclient import TestClient

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app import asgi
from app.controllers.v1 import settings as settings_controller
from app.utils import utils


class TestLlmModelCatalogEndpoint(unittest.TestCase):
    """
    模型目录接口只服务于设置页的下拉框：拿不到列表时必须安静降级，
    让用户继续手填模型 ID。
    """

    def setUp(self):
        self.client = TestClient(asgi.app)

    def _get(self, query: str):
        return self.client.get(f"/api/v1/metadata/llm-models{query}")

    def test_serves_the_catalogue_for_a_provider_that_publishes_one(self):
        catalog = {"models": [{"id": "openai/gpt-5.5", "label": "GPT-5.5"}], "error": None}

        with patch.object(
            settings_controller.llm_catalog, "list_models", return_value=catalog
        ) as list_models:
            response = self._get("?provider=openrouter")

        list_models.assert_called_once_with("openrouter")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["provider_id"], "openrouter")
        self.assertEqual(data["models"], catalog["models"])
        self.assertIsNone(data["error"])

    def test_a_catalogue_that_could_not_be_fetched_is_not_an_error_response(self):
        """
        失败必须以 200 带 error 的形式返回。抛 4xx/5xx 会在设置页顶部弹出
        红色横幅，而实际上用户只是需要下面那个手填输入框。
        """
        catalog = {"models": [], "error": "OSError: offline"}

        with patch.object(
            settings_controller.llm_catalog, "list_models", return_value=catalog
        ):
            response = self._get("?provider=openrouter")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["error"], "OSError: offline")

    def test_an_unknown_provider_is_rejected(self):
        """未知 Provider 是调用方的错误，不能悄悄返回空列表掩盖过去。"""
        for query in ("?provider=not-a-provider", ""):
            with self.subTest(query=query):
                response = self._get(query)
                self.assertEqual(response.status_code, 400)

    def test_the_provider_id_is_matched_case_insensitively(self):
        """配置里保存的是小写 ID，但界面和手工调用都可能传大写。"""
        with patch.object(
            settings_controller.llm_catalog,
            "list_models",
            return_value={"models": [], "error": None},
        ) as list_models:
            response = self._get("?provider=OpenRouter")

        self.assertEqual(response.status_code, 200)
        list_models.assert_called_once_with("openrouter")


class TestLlmProviderMetadata(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(asgi.app)

    def test_reports_which_providers_have_a_catalogue(self):
        """
        设置页据此决定是否请求模型列表；没有目录的 Provider 不应该为一个
        注定为空的请求付出一次往返。
        """
        response = self.client.get("/api/v1/metadata/llm-providers")

        self.assertEqual(response.status_code, 200)
        catalogs = {
            provider["provider_id"]: provider["has_model_catalog"]
            for provider in response.json()["data"]["providers"]
        }
        self.assertTrue(catalogs["openrouter"])
        self.assertFalse(catalogs["openai"])


if __name__ == "__main__":
    unittest.main()


class TestVoiceMetadataEndpoint(unittest.TestCase):
    """
    音色接口现在按引擎分别作答。此前它把 Edge 免费音色和 Azure 付费 V2 音色
    混在一个列表里，而 ElevenLabs 根本没有入口——用户在设置里填好 Key，
    却在界面上找不到任何可选的声音。
    """

    def setUp(self):
        self.client = TestClient(asgi.app)

    def _get(self, query: str = ""):
        return self.client.get(f"/api/v1/metadata/voices{query}")

    def test_edge_leaves_out_the_paid_multilingual_voices(self):
        """
        V2 音色走的是另一套需要付费凭据的 API。混在免费列表里，用户只会在
        渲染失败时才发现这件事。
        """
        response = self._get("?engine=edge&locale=en-US")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["voices"])
        self.assertFalse([v for v in data["voices"] if "-V2" in v])
        # Free: nothing to configure, so nothing to warn about.
        self.assertTrue(data["configured"])
        self.assertEqual(data["message"], "")

    def test_the_default_engine_is_the_free_one(self):
        """不带参数的老调用方必须继续拿到免费音色，而不是一个空列表。"""
        default = self._get("?locale=en-US").json()["data"]
        explicit = self._get("?engine=edge&locale=en-US").json()["data"]

        self.assertEqual(default["engine"], "edge")
        self.assertEqual(default["voices"], explicit["voices"])

    def test_azure_offers_only_the_v2_voices_and_ignores_the_locale(self):
        """
        九个 V2 音色都是多语言的。按界面上的语言下拉去过滤，只会让除四个
        语言之外的每次选择都得到空列表。
        """
        response = self._get("?engine=azure&locale=ro-RO")

        data = response.json()["data"]
        self.assertTrue(data["voices"])
        self.assertTrue(all("-V2" in voice for voice in data["voices"]))

    def test_azure_without_credentials_says_what_is_missing(self):
        with patch.object(
            settings_controller.voice_service, "azure_speech_configured",
            return_value=False,
        ):
            data = self._get("?engine=azure").json()["data"]

        self.assertFalse(data["configured"])
        self.assertIn("Settings", data["message"])
        # The voices are still listed: they are what the key would buy.
        self.assertTrue(data["voices"])

    def test_elevenlabs_lists_the_account_voices(self):
        with (
            patch.object(
                settings_controller.voice_service, "get_elevenlabs_api_key",
                return_value="a-key",
            ),
            patch.object(
                settings_controller.voice_service, "fetch_elevenlabs_voices",
                return_value=(["elevenlabs:abc:Adam"], ""),
            ) as fetch,
        ):
            data = self._get("?engine=elevenlabs").json()["data"]

        fetch.assert_called_once_with("a-key")
        self.assertEqual(data["voices"], ["elevenlabs:abc:Adam"])
        self.assertTrue(data["configured"])

    def test_elevenlabs_reports_why_the_list_is_empty(self):
        """
        Key 没填、Key 被拒、服务不可达，在界面上都是同一个空下拉框。
        用户唯一能做的就是反复改一个本来就没问题的设置。
        """
        with (
            patch.object(
                settings_controller.voice_service, "get_elevenlabs_api_key",
                return_value="bad-key",
            ),
            patch.object(
                settings_controller.voice_service, "fetch_elevenlabs_voices",
                return_value=([], "ElevenLabs rejected the API key."),
            ),
        ):
            data = self._get("?engine=elevenlabs").json()["data"]

        self.assertEqual(data["voices"], [])
        # Configured, and still unusable: the message is the only thing that
        # tells those two apart.
        self.assertTrue(data["configured"])
        self.assertEqual(data["message"], "ElevenLabs rejected the API key.")

    def test_an_unknown_engine_is_rejected(self):
        response = self._get("?engine=gramophone")

        self.assertEqual(response.status_code, 400)


class TestFontFileEndpoint(unittest.TestCase):
    """
    字幕预览必须能拿到真正的字体文件。

    之前只有字体"列表"接口：用户在下拉框里换字体，预览里的字形却一直是
    系统黑体，而字形正是预览存在的理由——烧进视频里的字幕改不回来。
    """

    def setUp(self):
        self.client = TestClient(asgi.app)
        self.font_dir = tempfile.mkdtemp()
        Path(self.font_dir, "Real-Bold.ttf").write_bytes(b"ttf-bytes")
        Path(self.font_dir, "Old-Collection.ttc").write_bytes(b"ttc-bytes")
        Path(self.font_dir, "secret.toml").write_bytes(b"api_key = 'nope'")

    def tearDown(self):
        shutil.rmtree(self.font_dir, ignore_errors=True)

    def _get(self, name: str):
        with patch.object(
            settings_controller.utils, "font_dir", return_value=self.font_dir
        ):
            return self.client.get(f"/api/v1/metadata/fonts/{name}")

    def test_serves_a_bundled_font_byte_for_byte(self):
        response = self._get("Real-Bold.ttf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "font/ttf")
        self.assertEqual(response.content, b"ttf-bytes")

    def test_a_collection_the_browser_cannot_load_is_not_served(self):
        """
        .ttc 浏览器加载不了。给它 200 只会让预览拿到一段永远解析失败的
        字节流，还不如 404，让前端老老实实回退到后备字体。
        """
        self.assertEqual(self._get("Old-Collection.ttc").status_code, 404)

    def test_the_endpoint_does_not_hand_out_other_files_in_the_directory(self):
        self.assertEqual(self._get("secret.toml").status_code, 404)

    def test_a_path_escaping_the_font_directory_is_refused(self):
        response = self._get("..%2F..%2Fconfig.toml")

        self.assertIn(response.status_code, (403, 404))

    def test_a_font_that_is_not_there_is_a_404(self):
        self.assertEqual(self._get("Missing-Bold.ttf").status_code, 404)


class TestSettingsWriteAllowlist(unittest.TestCase):
    """
    PUT /settings 只允许修改设置表单真正编辑的键。

    config.toml 不是惰性数据：ffmpeg_path 指向渲染流水线会执行的二进制，
    tls_verify 决定携带 API Key 的出站请求是否校验证书，redis_* 决定任务
    管理器连接到哪台主机。这些必须留在配置文件里手工修改。
    """

    def setUp(self):
        self.client = TestClient(asgi.app)
        self.config = settings_controller.config
        self.originals = {
            name: dict(section)
            for name, section in settings_controller._WRITABLE_SECTIONS.items()
        }
        # 真正落盘会重写开发机上的 config.toml 并丢掉其中全部注释。这些用例
        # 只需要验证内存中的配置有没有被改动。
        save_config = patch.object(self.config, "save_config")
        self.save_config = save_config.start()
        self.addCleanup(save_config.stop)

    def tearDown(self):
        for name, values in self.originals.items():
            section = settings_controller._WRITABLE_SECTIONS[name]
            section.clear()
            section.update(values)

    def _put(self, body):
        return self.client.put("/api/v1/settings", json=body)

    def test_ffmpeg_path_cannot_be_changed_through_the_api(self):
        """写入 ffmpeg_path 等于让下次启动执行调用方指定的任意程序。"""

        self.config.app["ffmpeg_path"] = ""

        response = self._put({"app": {"ffmpeg_path": "C:/Windows/System32/calc.exe"}})

        self.assertEqual(response.status_code, 400)
        self.assertIn("ffmpeg_path", response.json()["message"])
        self.assertEqual(self.config.app["ffmpeg_path"], "")
        # 拒绝必须发生在写配置之前，不能先落盘再报错。
        self.save_config.assert_not_called()

    def test_tls_verify_cannot_be_disabled_through_the_api(self):
        self.config.app["tls_verify"] = True

        response = self._put({"app": {"tls_verify": False}})

        self.assertEqual(response.status_code, 400)
        self.assertIs(self.config.app["tls_verify"], True)

    def test_the_redis_connection_cannot_be_redirected(self):
        self.config.app["redis_host"] = "localhost"

        response = self._put(
            {"app": {"enable_redis": True, "redis_host": "attacker.example"}}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.config.app["redis_host"], "localhost")

    def test_an_unknown_key_is_refused_rather_than_stored(self):
        """接受未知键会把无人读取的内容写进 config.toml，看起来像是生效了。"""

        response = self._put({"app": {"not_a_real_setting": "x"}})

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("not_a_real_setting", self.config.app)

    def test_a_nested_value_is_refused_for_a_writable_key(self):
        """嵌套表无法被表单渲染，也无法由 save_config 原样写回。"""

        response = self._put({"app": {"llm_provider": {"nested": 1}}})

        self.assertEqual(response.status_code, 400)
        self.assertIn("llm_provider", response.json()["message"])

    def test_a_whole_section_posted_back_unchanged_is_accepted(self):
        """
        表单读取整段配置再原样提交，因此每次保存都会带上它并不编辑的键，
        包括受保护的那些。如果按“请求里出现即拒绝”实现，任何真实配置都将
        永远无法保存——这条用例就是为了钉住这一点。
        """

        self.config.app["ffmpeg_path"] = "C:/tools/ffmpeg.exe"
        self.config.app["tls_verify"] = True
        current = self.client.get("/api/v1/settings").json()["data"]

        response = self._put(
            {name: current[name] for name in settings_controller._WRITABLE_SECTIONS}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config.app["ffmpeg_path"], "C:/tools/ffmpeg.exe")

    def test_a_provider_credential_is_writable(self):
        response = self._put({"app": {"openrouter_api_key": "sk-test-value"}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config.app["openrouter_api_key"], "sk-test-value")
        self.save_config.assert_called()

    def test_every_registered_provider_can_have_its_credentials_written(self):
        """
        表单从注册表渲染输入框，允许列表也从同一处派生。任何一个 Provider
        对不上，用户看到的都是一个能编辑却存不下去的字段。
        """

        writable = settings_controller._WRITABLE_KEYS["app"]
        for provider in settings_controller.LLM_PROVIDER_REGISTRY:
            with self.subTest(provider=provider.provider_id):
                for suffix in ("api_key", "model_name", "base_url"):
                    self.assertIn(provider.config_key(suffix), writable)
                for field in provider.extra_fields:
                    self.assertIn(provider.config_key(field.config_suffix), writable)

    def test_settings_allowlist_matches_the_shipped_example(self):
        """
        凭据分区是手写的列表。config.example.toml 是真正发布出去的文件，
        两者一旦漂移，用户就会遇到界面里存在、保存却被拒绝的设置项。
        """

        example_path = Path(utils.resource_dir()) / "config.example.toml"
        example = toml.load(example_path)

        for section in ("azure", "siliconflow", "minimax_tts", "elevenlabs", "chatterbox", "ui"):
            with self.subTest(section=section):
                self.assertEqual(
                    settings_controller._WRITABLE_KEYS[section],
                    frozenset(example[section]),
                )
