import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.services import material, scene


class TestDownloadCacheMirroring(unittest.TestCase):
    """
    下载的素材必须同时留在共享缓存里，否则"已下载片段"永远是空的。

    评审流程会把每个片段强制写进任务目录（预览只能从那里播放），而评审
    是默认路径。结果是 cache_videos 从来没有内容：Library 的 Downloaded
    clips 一直是空的，"Reuse from your library" 也没有东西可选，同一段
    素材每次渲染都要重新下载、重新消耗配额。
    """

    class _FakeClip:
        duration = 1
        fps = 24

        def __init__(self, path):
            self.path = path

        def close(self):
            return None

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.cache_dir = os.path.join(self.temp_root, "cache_videos")
        self.task_dir = os.path.join(self.temp_root, "tasks", "task-a")
        os.makedirs(self.cache_dir)
        os.makedirs(self.task_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _save_into(self, save_dir):
        fake_response = SimpleNamespace(content=b"fake-video")
        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch(
            "app.services.material.VideoFileClip", self._FakeClip
        ), patch(
            "app.services.material.utils.storage_dir", return_value=self.cache_dir
        ):
            return material.save_video(
                "https://example.com/clip.mp4?token=abc", save_dir=save_dir
            )

    def test_clip_saved_to_task_directory_also_lands_in_the_cache(self):
        video_path = self._save_into(self.task_dir)

        self.assertEqual(os.path.dirname(video_path), self.task_dir)
        cached = os.path.join(self.cache_dir, os.path.basename(video_path))
        self.assertTrue(
            os.path.exists(cached),
            "a reviewed download never reached the shared cache",
        )
        self.assertEqual(os.path.getsize(cached), os.path.getsize(video_path))

    def test_deleting_the_cached_copy_leaves_the_render_its_footage(self):
        """
        清理缓存不能把正在使用的任务素材一起删掉——两者是同一份数据的两个
        名字，删掉其中一个，另一个必须照常可读。
        """
        video_path = self._save_into(self.task_dir)
        cached = os.path.join(self.cache_dir, os.path.basename(video_path))

        os.remove(cached)

        self.assertTrue(os.path.exists(video_path))
        self.assertEqual(Path(video_path).read_bytes(), b"fake-video")

    def test_clip_already_in_the_cache_is_not_mirrored_onto_itself(self):
        video_path = self._save_into(self.cache_dir)

        self.assertEqual(os.path.dirname(video_path), self.cache_dir)
        self.assertEqual(len(os.listdir(self.cache_dir)), 1)


class TestMaterialTlsVerification(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def test_search_pexels_uses_tls_verification_by_default(self):
        """
        默认路径必须开启 TLS 校验，避免素材 API key 和返回的素材 URL
        在公共网络或不可信代理环境中被中间人攻击截获或篡改。
        """
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "id": 321,
                        "url": "https://www.pexels.com/video/example-321/?token=drop",
                        "duration": 8,
                        "user": {
                            "id": 654,
                            "name": "Pexels Creator",
                            "url": "https://www.pexels.com/@creator/?key=drop",
                        },
                        "video_files": [
                            {
                                "id": 987,
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/video.mp4",
                            }
                        ],
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pexels("cat", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertTrue(get.call_args.kwargs["verify"])
        self.assertEqual(results[0].source_info["asset_id"], "321")
        self.assertEqual(
            results[0].source_info["source_page"],
            "https://www.pexels.com/video/example-321/",
        )
        self.assertEqual(
            results[0].source_info["creator"]["profile_page"],
            "https://www.pexels.com/@creator/",
        )
        self.assertEqual(results[0].source_info["rendition"]["id"], "987")

    def test_search_pixabay_allows_explicit_tls_disable_for_proxy(self):
        """
        少数企业代理会使用自签证书。该场景必须显式配置关闭 TLS 校验，
        不能再由代码硬编码默认关闭。
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {
                "hits": [
                    {
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1080,
                                "url": "https://example.com/video.mp4",
                            }
                        },
                    }
                ]
            }
        )

        with patch("app.services.material.requests.get", return_value=fake_response) as get:
            results = material.search_videos_pixabay(
                "cat",
                minimum_duration=1,
                video_aspect=material.VideoAspect.landscape,
            )

        self.assertEqual(len(results), 1)
        self.assertFalse(get.call_args.kwargs["verify"])

    def test_remote_searches_only_return_requested_orientation(self):
        """
        三个素材源都必须只返回目标方向的素材，避免竖屏任务混入横屏素材后
        通过 letterbox 产生明显黑边。Pexels 使用远端参数并在本地校验，
        Pixabay 和 Coverr 使用响应尺寸做本地过滤。
        """
        config.app["pexels_api_keys"] = ["pexels-key"]
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()

        pexels_response = SimpleNamespace(
            json=lambda: {
                "videos": [
                    {
                        "id": 1,
                        "duration": 8,
                        "video_files": [
                            {
                                "id": 11,
                                "width": 1920,
                                "height": 1080,
                                "link": "https://example.com/landscape.mp4",
                            }
                        ],
                    },
                    {
                        "id": 2,
                        "duration": 8,
                        "video_files": [
                            {
                                "id": 22,
                                "width": 1080,
                                "height": 1920,
                                "link": "https://example.com/portrait.mp4",
                            }
                        ],
                    },
                ]
            }
        )
        pixabay_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {
                "hits": [
                    {
                        "id": 1,
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1080,
                                "url": "https://example.com/landscape.mp4",
                            }
                        },
                    },
                    {
                        "id": 2,
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1080,
                                "height": 1920,
                                "url": "https://example.com/portrait.mp4",
                            }
                        },
                    },
                ]
            },
        )
        coverr_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "landscape",
                        "duration": 8,
                        "max_width": 1920,
                        "max_height": 1080,
                        "urls": {
                            "mp4_download": "https://example.com/landscape.mp4"
                        },
                    },
                    {
                        "id": "portrait",
                        "duration": 8,
                        "max_width": 1080,
                        "max_height": 1920,
                        "urls": {
                            "mp4_download": "https://example.com/portrait.mp4"
                        },
                    },
                    {
                        "id": "unknown",
                        "duration": 8,
                        "urls": {"mp4_download": "https://example.com/unknown.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get",
            return_value=pexels_response,
        ) as get:
            pexels_results = material.search_videos_pexels(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.portrait,
            )
            pexels_url = get.call_args.args[0]
        with patch(
            "app.services.material.requests.get",
            return_value=pixabay_response,
        ):
            pixabay_results = material.search_videos_pixabay(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.portrait,
            )
        with patch(
            "app.services.material.requests.get",
            return_value=coverr_response,
        ) as get:
            coverr_results = material.search_videos_coverr(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.portrait,
            )
            coverr_url = get.call_args.args[0]

        self.assertIn("/v1/videos/search?", pexels_url)
        self.assertIn("orientation=portrait", pexels_url)
        self.assertIn("page_size=20", coverr_url)
        self.assertIn("filter=is_vertical%3Atrue", coverr_url)
        for results in (pexels_results, pixabay_results, coverr_results):
            self.assertEqual(
                [item.url for item in results],
                ["https://example.com/portrait.mp4"],
            )

    def test_video_aspect_matching_rejects_unknown_dimensions(self):
        """无法确认方向的素材不能进入严格的横竖屏候选列表。"""
        self.assertTrue(
            material._matches_video_aspect(
                1080,
                1920,
                material.VideoAspect.portrait,
            )
        )
        self.assertFalse(
            material._matches_video_aspect(
                1920,
                1080,
                material.VideoAspect.portrait,
            )
        )
        self.assertTrue(
            material._matches_video_aspect(
                None,
                None,
                material.VideoAspect.portrait,
                is_vertical=True,
            )
        )
        self.assertFalse(
            material._matches_video_aspect(
                None,
                None,
                material.VideoAspect.portrait,
            )
        )
        self.assertTrue(
            material._matches_video_aspect(
                1080,
                1080,
                material.VideoAspect.square,
            )
        )
        self.assertFalse(
            material._matches_video_aspect(
                1080,
                1920,
                material.VideoAspect.square,
            )
        )

    def test_coverr_passes_orientation_filter_to_remote_search(self):
        """Coverr 横竖屏搜索应在服务端筛选，方形素材继续使用本地尺寸校验。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        fake_response = SimpleNamespace(json=lambda: {"hits": []})
        cases = (
            (material.VideoAspect.portrait, "filter=is_vertical%3Atrue"),
            (material.VideoAspect.landscape, "filter=is_vertical%3Afalse"),
            (material.VideoAspect.square, None),
        )

        for aspect, expected_filter in cases:
            with self.subTest(aspect=aspect), patch(
                "app.services.material.requests.get",
                return_value=fake_response,
            ) as get:
                material.search_videos_coverr(
                    "city",
                    minimum_duration=1,
                    video_aspect=aspect,
                )
                request_url = get.call_args.args[0]

            self.assertIn("page_size=20", request_url)
            if expected_filter:
                self.assertIn(expected_filter, request_url)
            else:
                self.assertNotIn("filter=", request_url)

    def test_square_search_preserves_crop_compatible_materials(self):
        """
        Pixabay 和 Coverr 很少提供原生方形视频。方形输出必须继续接受可裁剪的
        横屏素材，否则选择这两个来源时会在搜索阶段直接得到空列表。
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.proxy.clear()
        pixabay_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {
                "hits": [
                    {
                        "id": 1,
                        "duration": 8,
                        "videos": {
                            "large": {
                                "width": 1920,
                                "height": 1080,
                                "url": "https://example.com/pixabay-landscape.mp4",
                            }
                        },
                    }
                ]
            },
        )
        coverr_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "landscape",
                        "duration": 8,
                        "max_width": 1920,
                        "max_height": 1080,
                        "urls": {
                            "mp4_download": "https://example.com/coverr-landscape.mp4"
                        },
                    }
                ]
            }
        )

        with patch(
            "app.services.material.requests.get",
            return_value=pixabay_response,
        ):
            pixabay_results = material.search_videos_pixabay(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.square,
            )
        with patch(
            "app.services.material.requests.get",
            return_value=coverr_response,
        ):
            coverr_results = material.search_videos_coverr(
                "city",
                minimum_duration=1,
                video_aspect=material.VideoAspect.square,
            )

        self.assertEqual(
            [item.url for item in pixabay_results],
            ["https://example.com/pixabay-landscape.mp4"],
        )
        self.assertEqual(
            [item.url for item in coverr_results],
            ["https://example.com/coverr-landscape.mp4"],
        )

    def test_search_pixabay_does_not_log_api_key(self):
        config.app["pixabay_api_keys"] = ["pixabay-secret-key"]
        config.proxy.clear()

        fake_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            text="",
            json=lambda: {"hits": []},
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch("app.services.material.logger.info") as log:
            material.search_videos_pixabay("cat", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertNotIn("pixabay-secret-key", logged_messages)

    def test_search_pixabay_reports_cloudflare_challenge(self):
        """
        Cloudflare Challenge 返回的是 HTML，不是 Pixabay API 的 JSON。
        应直接说明服务端拦截原因，避免用户只看到没有上下文的 JSON 解析错误。
        """
        config.app["pixabay_api_keys"] = ["pixabay-secret-key"]
        config.proxy.clear()

        fake_response = SimpleNamespace(
            status_code=429,
            headers={
                "content-type": "text/html; charset=UTF-8",
                "cf-mitigated": "challenge",
                "cf-ray": "test-ray",
            },
            text="<html><title>Just a moment...</title></html>",
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("Cloudflare challenge", logged_messages)
        self.assertIn("cf_ray=test-ray", logged_messages)
        self.assertNotIn("pixabay-secret-key", logged_messages)
        self.assertNotIn("Just a moment", logged_messages)

    def test_search_pixabay_reports_api_rate_limit(self):
        """
        Pixabay 自身的 429 限流与 Cloudflare HTML Challenge 是不同问题。
        保留 Retry-After 可以帮助用户判断何时重试，同时不记录响应正文。
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()

        fake_response = SimpleNamespace(
            status_code=429,
            headers={
                "content-type": "text/plain; charset=UTF-8",
                "retry-after": "60",
            },
            text="API rate limit exceeded",
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("API rate limit exceeded", logged_messages)
        self.assertIn("retry_after=60", logged_messages)

    def test_search_pixabay_reports_non_json_response(self):
        """
        即使状态码为 200，上游代理也可能返回登录页或其他非 JSON 内容。
        该场景应记录响应类型，而不是向外暴露底层 JSONDecodeError。
        """
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()

        def raise_invalid_json():
            raise ValueError("Expecting value: line 1 column 1")

        fake_response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "text/plain"},
            text="unexpected response",
            json=raise_invalid_json,
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("unexpected non-JSON response", logged_messages)
        self.assertNotIn("Expecting value", logged_messages)

    def test_search_pixabay_redacts_api_key_from_network_error(self):
        """
        requests 的连接异常可能回显完整请求 URL。异常详情仍应保留用于排查，
        但 URL 查询参数中的 Pixabay API Key 必须在写入日志前脱敏。
        """
        api_key = "pixabay-secret-key"
        config.app["pixabay_api_keys"] = [api_key]
        config.proxy.clear()
        error = requests.ConnectionError(
            "request failed for "
            f"https://pixabay.com/api/videos/?q=nature&key={api_key}"
        )

        with patch(
            "app.services.material.requests.get", side_effect=error
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("ConnectionError", logged_messages)
        self.assertIn("key=***", logged_messages)
        self.assertNotIn(api_key, logged_messages)

    def test_search_pixabay_redacts_proxy_credentials_from_network_error(self):
        """
        代理连接异常可能回显含认证信息的完整代理 URL。日志应保留异常类型，
        但不能把代理用户名和密码持久化到日志文件。
        """
        proxy_url = "http://proxy-user:proxy-password@proxy.example.com:8080"
        config.app["pixabay_api_keys"] = ["pixabay-key"]
        config.proxy.clear()
        config.proxy["http"] = proxy_url
        error = requests.exceptions.ProxyError(
            f"failed to connect to proxy {proxy_url}"
        )

        with patch(
            "app.services.material.requests.get", side_effect=error
        ), patch("app.services.material.logger.error") as log:
            results = material.search_videos_pixabay("nature", minimum_duration=1)

        logged_messages = " ".join(str(call.args[0]) for call in log.call_args_list)
        self.assertEqual(results, [])
        self.assertIn("ProxyError", logged_messages)
        self.assertNotIn("proxy-user", logged_messages)
        self.assertNotIn("proxy-password", logged_messages)

    def test_save_video_uses_tls_verification_by_default(self):
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(content=b"fake-video")

        class FakeVideoFileClip:
            duration = 1
            fps = 24

            def __init__(self, path):
                self.path = path

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = os.path.join(temp_dir, "cache_videos")
            os.makedirs(cache_dir)
            save_dir = os.path.join(temp_dir, "task-a")
            os.makedirs(save_dir)
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ) as get, patch(
                "app.services.material.VideoFileClip", FakeVideoFileClip
            ), patch(
                "app.services.material.utils.storage_dir", return_value=cache_dir
            ):
                video_path = material.save_video(
                    "https://example.com/video.mp4?token=abc", save_dir=save_dir
                )

            self.assertTrue(os.path.exists(video_path))
            self.assertTrue(get.call_args.kwargs["verify"])

    def test_download_videos_accepts_plain_string_concat_mode(self):
        """
        download_videos 可能被服务层或测试直接传入字符串模式，而不是
        VideoConcatMode 枚举。这里用空搜索词避免真实网络请求，只验证
        字符串 "random" 不会再因为访问 `.value` 抛 AttributeError。
        """
        result = material.download_videos(
            task_id="string-concat-mode",
            search_terms=[],
            video_concat_mode="random",
        )

        self.assertEqual(result, [])

    def test_material_source_record_uses_public_whitelist(self):
        """
        任务清单只应包含可追溯的公开字段，不能写入签名参数、下载地址、
        调用方传入的额外字段或本机绝对路径。
        """
        item = material.MaterialInfo(
            provider="pixabay",
            url="https://cdn.example.com/video.mp4?token=secret",
            duration=12,
            source_info={
                "provider": "pixabay",
                "search_term": "city",
                "asset_id": 123,
                "source_page": "https://pixabay.com/videos/city-123/?key=secret",
                "creator": {
                    "id": 456,
                    "name": "Creator",
                    "profile_page": "https://pixabay.com/users/creator/?token=secret",
                    "email": "private@example.com",
                },
                "rendition": {
                    "id": "large",
                    "width": 1920,
                    "height": 1080,
                    "download_url": "https://cdn.example.com/private",
                },
                "api_key": "must-not-persist",
            },
        )

        record = material._material_source_record(
            item,
            "/Users/example/private/task/vid-123.mp4",
        )
        serialized = str(record)

        self.assertEqual(record["local_file"], "vid-123.mp4")
        self.assertEqual(
            record["source_page"],
            "https://pixabay.com/videos/city-123/",
        )
        self.assertEqual(
            record["creator"]["profile_page"],
            "https://pixabay.com/users/creator/",
        )
        self.assertEqual(
            record["rendition"],
            {"id": "large", "width": 1920, "height": 1080},
        )
        self.assertNotIn("secret", serialized)
        self.assertNotIn("/Users/example", serialized)
        self.assertNotIn("private@example.com", serialized)

    def test_download_videos_can_round_robin_terms_in_script_order(self):
        """
        开启按文案顺序匹配素材后，不能让第一个关键词的多个候选先把
        音频时长填满。这里模拟两个关键词各有多个候选，验证下载顺序是
        term1-第1个、term2-第1个、term1-第2个，贴近脚本叙事顺序。
        """
        search_results = {
            "opening city": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/a1.mp4",
                    duration=3,
                    source_info={
                        "provider": "pexels",
                        "search_term": "opening city",
                        "asset_id": "a1",
                    },
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/a2.mp4",
                    duration=3,
                    source_info={
                        "provider": "pexels",
                        "search_term": "opening city",
                        "asset_id": "a2",
                    },
                ),
            ],
            "middle office": [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/b1.mp4",
                    duration=3,
                    source_info={
                        "provider": "pexels",
                        "search_term": "middle office",
                        "asset_id": "b1",
                    },
                ),
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/b2.mp4",
                    duration=3,
                    source_info={
                        "provider": "pexels",
                        "search_term": "middle office",
                        "asset_id": "b2",
                    },
                ),
            ],
        }
        downloaded_urls = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return search_results[search_term]

        def fake_save_video(video_url, save_dir=""):
            downloaded_urls.append(video_url)
            return f"/tmp/{video_url.rsplit('/', 1)[-1]}"

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                return_value=True,
            ) as patch_script,
        ):
            result = material.download_videos(
                task_id="ordered-materials",
                search_terms=["opening city", "middle office"],
                source="pexels",
                audio_duration=7,
                max_clip_duration=3,
                match_script_order=True,
            )

        self.assertEqual(
            downloaded_urls,
            [
                "https://v.example/a1.mp4",
                "https://v.example/b1.mp4",
                "https://v.example/a2.mp4",
            ],
        )
        self.assertEqual(result, ["/tmp/a1.mp4", "/tmp/b1.mp4", "/tmp/a2.mp4"])
        recorded_sources = patch_script.call_args.kwargs["material_sources"]
        self.assertEqual(
            [source["asset_id"] for source in recorded_sources],
            ["a1", "b1", "a2"],
        )
        self.assertEqual(
            [source["local_file"] for source in recorded_sources],
            ["a1.mp4", "b1.mp4", "a2.mp4"],
        )

    def test_a_long_narration_stops_at_the_clip_ceiling(self):
        """
        69 秒旁白按“每 5 秒一个片段”会下载 15 个文件。默认的随机场景计划
        配合每分钟七个片段的上限，必须把同一条视频压到个位数，
        并且不依赖候选素材耗尽。
        """
        candidates = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/clip-{index}.mp4",
                duration=40,
                source_info={
                    "provider": "pexels",
                    "search_term": "wild animals",
                    "asset_id": str(index),
                },
            )
            for index in range(40)
        ]
        downloaded = []

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(
                material, "search_videos_pexels", side_effect=lambda **kwargs: candidates
            ),
            patch.object(
                material,
                "save_video",
                side_effect=lambda video_url, save_dir="": (
                    downloaded.append(video_url) or f"/tmp/{len(downloaded)}.mp4"
                ),
            ),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts, "patch_script_data", return_value=True
            ),
        ):
            result = material.download_videos(
                task_id="long-narration",
                search_terms=["wild animals"],
                source="pexels",
                audio_duration=69,
                max_clip_duration=scene.VARIED_SCENE_LENGTH,
            )

        self.assertLessEqual(len(result), scene.max_material_count(69))
        self.assertLess(len(result), 15)
        self.assertGreaterEqual(len(result), 5)

    def test_a_pinned_scene_length_still_downloads_one_clip_per_scene(self):
        """
        显式选择“每个镜头 5 秒”的用户要的就是每分钟十二个镜头，
        上限不能替他改成七个。
        """
        candidates = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/fixed-{index}.mp4",
                duration=30,
                source_info={
                    "provider": "pexels",
                    "search_term": "city",
                    "asset_id": str(index),
                },
            )
            for index in range(20)
        ]
        downloaded = []

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(
                material, "search_videos_pexels", side_effect=lambda **kwargs: candidates
            ),
            patch.object(
                material,
                "save_video",
                side_effect=lambda video_url, save_dir="": (
                    downloaded.append(video_url) or f"/tmp/{len(downloaded)}.mp4"
                ),
            ),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts, "patch_script_data", return_value=True
            ),
        ):
            result = material.download_videos(
                task_id="pinned-scenes",
                search_terms=["city"],
                source="pexels",
                audio_duration=30,
                max_clip_duration=5,
            )

        self.assertEqual(len(result), 7)

    def test_material_source_persistence_failure_does_not_break_download(self):
        """辅助任务记录失败时，已经下载成功的素材仍应正常返回给成片主流程。"""
        item = material.MaterialInfo(
            provider="pexels",
            url="https://v.example/a1.mp4",
            duration=5,
            source_info={"provider": "pexels", "asset_id": "a1"},
        )

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", return_value=[item]),
            patch.object(material, "save_video", return_value="/tmp/a1.mp4"),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts,
                "patch_script_data",
                side_effect=OSError("disk unavailable"),
            ),
            patch.object(material.logger, "warning") as warning,
        ):
            result = material.download_videos(
                task_id="persist-failure",
                search_terms=["city"],
                source="pexels",
                audio_duration=1,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["/tmp/a1.mp4"])
        self.assertTrue(warning.called)


class TestCoverrProvider(unittest.TestCase):
    """
    Coverr 视频素材源(spec: 2026-06-09-coverr-video-provider-design.md)。
    全部用 unittest.mock 替换 requests，确保 CI 不依赖真实网络和真实 API key。
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    # ---------------- Tests for search_videos_coverr ----------------

    def test_search_coverr_uses_mp4_download_url(self):
        """
        search_videos_coverr 应把每个 hit 转成 MaterialInfo，并把 urls.mp4_download
        直接作为 MaterialInfo.url。
        按 Coverr 官方文档 (api.coverr.co/docs/videos/#download-a-video),
        GET mp4_download 本身就被 Coverr 计入下载统计,无需额外 PATCH ping。
        同时验证 Authorization header 使用 Bearer scheme。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "page": 0,
                "pages": 50,
                "page_size": 20,
                "total": 1,
                "hits": [
                    {
                        "id": "S1YbPl1NfI",
                        "duration": 11.625,
                        "aspect_ratio": "16:9",
                        "canonical_url": "https://coverr.co/videos/example?token=drop",
                        "creator": {
                            "id": "creator-1",
                            "name": "Coverr Creator",
                            "profile_url": "https://coverr.co/creators/example?key=drop",
                        },
                        "max_width": 3840,
                        "max_height": 2160,
                        "urls": {
                            "mp4": "https://storage.coverr.co/videos/abc?token=xyz",
                            "mp4_preview": "https://storage.coverr.co/videos/abc/preview?token=xyz",
                            "mp4_download": "https://storage.coverr.co/videos/abc/download?token=xyz",
                        },
                    }
                ],
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            results = material.search_videos_coverr(
                "nature",
                minimum_duration=5,
                video_aspect=material.VideoAspect.landscape,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "coverr")
        self.assertEqual(item.duration, 11)
        # url 字段就是 mp4_download URL,不再做 coverr://id|url 编码
        self.assertEqual(
            item.url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )
        self.assertEqual(item.source_info["asset_id"], "S1YbPl1NfI")
        self.assertEqual(
            item.source_info["source_page"],
            "https://coverr.co/videos/example",
        )
        self.assertEqual(
            item.source_info["creator"]["profile_page"],
            "https://coverr.co/creators/example",
        )
        # Bearer auth + TLS verify on by default
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], "Bearer coverr-key"
        )
        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_uses_tls_verification_by_default(self):
        """与 pexels/pixabay 一致:未显式配置时 TLS 校验默认开启。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertTrue(get.call_args.kwargs["verify"])

    def test_search_coverr_allows_explicit_tls_disable_for_proxy(self):
        """企业自签证书代理场景必须能显式关闭 TLS 校验。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app["tls_verify"] = False
        config.proxy.clear()

        fake_response = SimpleNamespace(json=lambda: {"hits": []})

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ) as get:
            material.search_videos_coverr("nature", minimum_duration=1)

        self.assertFalse(get.call_args.kwargs["verify"])

    def test_search_coverr_filters_by_min_duration_and_accepts_string(self):
        """
        Coverr duration 字段在不同响应里可能是 number 或 string,
        两种格式都要接受;低于 minimum_duration 的应被过滤。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {
                        "id": "shortvid",
                        "duration": 3,  # below minimum
                        "urls": {"mp4_download": "https://example.com/a.mp4"},
                    },
                    {
                        "id": "stringdur",
                        "duration": "10.500000",  # string accepted
                        "max_width": 1080,
                        "max_height": 1920,
                        "urls": {"mp4_download": "https://example.com/b.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].duration, 10)
        self.assertEqual(results[0].url, "https://example.com/b.mp4")

    def test_search_coverr_skips_invalid_items(self):
        """缺 id 或缺 urls.mp4_download 的条目应被跳过,不应抛异常。"""
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        fake_response = SimpleNamespace(
            json=lambda: {
                "hits": [
                    {  # missing urls.mp4_download
                        "id": "no-download",
                        "duration": 10,
                        "urls": {"mp4_preview": "https://example.com/preview.mp4"},
                    },
                    {  # missing id
                        "duration": 10,
                        "urls": {"mp4_download": "https://example.com/x.mp4"},
                    },
                    {  # valid baseline
                        "id": "good",
                        "duration": 10,
                        "max_width": 1080,
                        "max_height": 1920,
                        "urls": {"mp4_download": "https://example.com/good.mp4"},
                    },
                ]
            }
        )

        with patch(
            "app.services.material.requests.get", return_value=fake_response
        ):
            results = material.search_videos_coverr("x", minimum_duration=1)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/good.mp4")

    def test_search_coverr_returns_empty_on_failure(self):
        """
        响应结构异常 / 网络异常时,函数必须返回 [] 而不是抛异常,
        与 pexels/pixabay 行为保持一致。
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

        # Subtest A: malformed response (no "hits" key)
        with self.subTest("malformed response"):
            fake_response = SimpleNamespace(
                json=lambda: {"error": "rate limited"}
            )
            with patch(
                "app.services.material.requests.get", return_value=fake_response
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

        # Subtest B: network exception bubbles up from requests.get
        with self.subTest("network exception"):
            with patch(
                "app.services.material.requests.get",
                side_effect=requests.ConnectionError("boom"),
            ):
                results = material.search_videos_coverr("x", minimum_duration=1)
            self.assertEqual(results, [])

    # ---------------- Tests for download_videos coverr branch ----------------

    def test_download_videos_passes_mp4_download_url_to_save_video(self):
        """
        在 source="coverr" 时:
          1. dispatch 到 search_videos_coverr
          2. coverr item 走通用下载路径:save_video 收到的就是 mp4_download URL
             (不再有 coverr://id|url 编码,也不再调用 PATCH ping)
          3. 返回保存路径
        """
        config.app["coverr_api_keys"] = ["coverr-key"]
        config.app.pop("tls_verify", None)
        config.app.pop("material_directory", None)
        config.proxy.clear()

        fake_item = material.MaterialInfo()
        fake_item.provider = "coverr"
        fake_item.url = "https://storage.coverr.co/videos/abc/download?token=xyz"
        fake_item.duration = 10

        with patch(
            "app.services.material.search_videos_coverr",
            return_value=[fake_item],
        ) as search, patch(
            "app.services.material.save_video",
            return_value="/tmp/coverr-saved.mp4",
        ) as save, patch(
            "app.services.material.material_cache.load_material_search_cache",
            return_value=None,
        ), patch(
            "app.services.material.material_cache.save_material_search_cache",
        ):
            result = material.download_videos(
                task_id="t-coverr",
                search_terms=["nature"],
                source="coverr",
                audio_duration=5,
                max_clip_duration=5,
            )

        # 1. dispatch
        self.assertEqual(search.call_count, 1)

        # 2. save_video 收到的就是 mp4_download URL,原样传入
        save_url = save.call_args.kwargs.get("video_url") or save.call_args.args[0]
        self.assertEqual(
            save_url, "https://storage.coverr.co/videos/abc/download?token=xyz"
        )

        # 3. 返回值正确
        self.assertEqual(result, ["/tmp/coverr-saved.mp4"])


class TestWaveSpeedProvider(unittest.TestCase):
    """
    WaveSpeed AI 文生视频素材源。与其它素材源测试一致,全部用 unittest.mock
    替换 requests 和 time.sleep,CI 不依赖真实网络、真实 API key 和真实计费。
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        config.app["wavespeed_api_keys"] = ["wavespeed-key"]
        config.app.pop("tls_verify", None)
        config.proxy.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    @staticmethod
    def _json_response(payload):
        return SimpleNamespace(json=lambda: payload)

    def test_generate_wavespeed_submits_and_polls_to_completion(self):
        """
        提交请求必须携带 Bearer 鉴权、模型 ID 路径和 prompt/aspect_ratio/duration
        三个生成参数;轮询到 completed 后把 outputs 转成 MaterialInfo。
        """
        submit_response = self._json_response(
            {"code": 200, "message": "success", "data": {"id": "pred-123"}}
        )
        poll_responses = [
            self._json_response(
                {"code": 200, "data": {"id": "pred-123", "status": "processing"}}
            ),
            self._json_response(
                {
                    "code": 200,
                    "data": {
                        "id": "pred-123",
                        "status": "completed",
                        "outputs": ["https://cdn.example.com/out.mp4?sig=abc"],
                    },
                }
            ),
        ]

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch(
                "app.services.material.requests.get", side_effect=poll_responses
            ) as get,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_videos_wavespeed(
                "sunrise over mountains",
                minimum_duration=5,
                video_aspect=material.VideoAspect.portrait,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "wavespeed")
        # 签名 URL 必须原样保留,查询参数不能被剥离,否则下载会 403
        self.assertEqual(item.url, "https://cdn.example.com/out.mp4?sig=abc")
        self.assertEqual(item.duration, 5)
        self.assertEqual(item.source_info["asset_id"], "pred-123")
        self.assertEqual(item.source_info["search_term"], "sunrise over mountains")
        # 生成产物地址是临时签名 URL,不允许写入来源记录
        self.assertNotIn("source_page", item.source_info)

        self.assertIn(
            "/api/v3/bytedance/seedance-2.0-fast/text-to-video",
            post.call_args.args[0],
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer wavespeed-key",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "prompt": "sunrise over mountains",
                "aspect_ratio": "9:16",
                "duration": 5,
            },
        )
        self.assertTrue(post.call_args.kwargs["verify"])
        self.assertIn("/api/v3/predictions/pred-123/result", get.call_args.args[0])
        # processing 状态下必须等待轮询间隔,不能空转打满远端接口
        self.assertEqual(sleep.call_count, 1)

    def test_generate_wavespeed_uses_configured_model_id(self):
        """用户可以在配置中切换任意 WaveSpeed 文生视频模型。"""
        config.app["wavespeed_text_to_video_model"] = "wavespeed-ai/custom-t2v"
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-9"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-9",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/a.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            results = material.generate_videos_wavespeed(
                "city timelapse",
                minimum_duration=3,
                video_aspect=material.VideoAspect.landscape,
            )

        self.assertEqual(len(results), 1)
        self.assertIn("/api/v3/wavespeed-ai/custom-t2v", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["aspect_ratio"], "16:9")

    def test_generate_wavespeed_returns_empty_on_failed_prediction(self):
        """failed/cancelled/timeout 都按空结果返回,让上层跳过该关键词继续。"""
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-fail"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-fail",
                    "status": "failed",
                    "error": "content policy",
                },
            }
        )

        with (
            patch("app.services.material.requests.post", return_value=submit_response),
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(results, [])

    def test_generate_wavespeed_returns_empty_on_rejected_submission(self):
        """非 200 envelope(如 key 无效)不能进入轮询,直接返回空结果。"""
        submit_response = self._json_response({"code": 401, "message": "invalid api key"})

        with (
            patch("app.services.material.requests.post", return_value=submit_response),
            patch("app.services.material.requests.get") as get,
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(results, [])
        get.assert_not_called()

    def test_generate_wavespeed_never_retries_submission_on_network_error(self):
        """
        提交没有收到响应不代表任务没有创建,重发 POST 会重复生成、重复扣费。
        因此提交绝不自动重试,并按"状态不明"上抛,让上层停止继续下单。
        """
        with patch(
            "app.services.material.requests.post",
            side_effect=requests.exceptions.ConnectionError("boom"),
        ) as post:
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError):
                material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(post.call_count, 1)

    def test_generate_wavespeed_treats_server_error_submission_as_unconfirmed(self):
        """5xx 可能发生在任务创建之后,状态不明,不能当作"没扣费"继续。"""
        submit_response = SimpleNamespace(
            status_code=502, json=lambda: {"code": 502, "message": "bad gateway"}
        )

        with patch("app.services.material.requests.post", return_value=submit_response):
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError):
                material.generate_videos_wavespeed("sunrise", minimum_duration=5)

    def test_generate_wavespeed_retries_transient_poll_failures_on_same_task(self):
        """
        轮询遇到 429/5xx 或网络异常时,必须带着原来的 prediction id 退避重试,
        绝不能重新提交一次付费生成任务。
        """
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-r1"}})
        rate_limited = SimpleNamespace(status_code=429, json=lambda: {"code": 429})
        completed = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-r1",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/r1.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch(
                "app.services.material.requests.get",
                side_effect=[
                    rate_limited,
                    requests.exceptions.ConnectionError("boom"),
                    completed,
                ],
            ) as get,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(len(results), 1)
        # 只提交一次;三次 GET 全部指向同一个 prediction id
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 3)
        for call in get.call_args_list:
            self.assertIn("/api/v3/predictions/pred-r1/result", call.args[0])
        # 线性退避:第 n 次重试等待 base * n
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])

    def test_generate_wavespeed_raises_unconfirmed_after_poll_retries_exhausted(self):
        """
        连续临时失败超过上限后,任务状态仍然不明:任务可能还在远端运行。
        必须上抛并带上 prediction id,而不是当作失败让流程继续下单。
        """
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-r2"}})

        with (
            patch("app.services.material.requests.post", return_value=submit_response),
            patch(
                "app.services.material.requests.get",
                side_effect=requests.exceptions.ConnectionError("boom"),
            ) as get,
            patch("app.services.material.time.sleep"),
        ):
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError) as ctx:
                material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(ctx.exception.prediction_id, "pred-r2")
        self.assertEqual(get.call_count, material.WAVESPEED_MAX_POLL_RETRIES + 1)

    def test_generate_wavespeed_raises_unconfirmed_on_local_wait_timeout(self):
        """本地等待超时,远端任务仍在运行,状态不明,不能继续提交新任务。"""
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-r3"}})
        processing = self._json_response(
            {"code": 200, "data": {"id": "pred-r3", "status": "processing"}}
        )
        clock = iter([0.0, 0.0, material.WAVESPEED_RUN_TIMEOUT_SECONDS + 1])

        with (
            patch("app.services.material.requests.post", return_value=submit_response),
            patch("app.services.material.requests.get", return_value=processing),
            patch(
                "app.services.material.time.monotonic", side_effect=lambda: next(clock)
            ),
            patch("app.services.material.time.sleep"),
        ):
            with self.assertRaises(material.WaveSpeedUnconfirmedTaskError) as ctx:
                material.generate_videos_wavespeed("sunrise", minimum_duration=5)

        self.assertEqual(ctx.exception.prediction_id, "pred-r3")

    def test_download_videos_wavespeed_stops_submitting_after_unconfirmed_task(self):
        """
        回归:某个片段的任务状态不明时,后续关键词绝不能再触发新的付费生成
        请求——否则第一个任务可能仍在运行/已完成,造成重复生成和额外扣费。
        已经下载成功的素材照常返回。
        """
        first_item = self._generated_item("term-1", "https://cdn.example.com/1.mp4")

        def fake_generate(search_term, minimum_duration, video_aspect):
            if search_term == "term-1":
                return [first_item]
            raise material.WaveSpeedUnconfirmedTaskError(
                "state unknown", prediction_id="pred-stuck"
            )

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material.save_video",
                return_value="/tmp/1.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-unconfirmed",
                search_terms=["term-1", "term-2", "term-3"],
                source="wavespeed",
                audio_duration=100,
                max_clip_duration=5,
            )

        # term-2 抛出状态不明后立即停止,term-3 不能再产生生成请求
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result, ["/tmp/1.mp4"])

    def test_download_videos_wavespeed_retries_original_download_url(self):
        """
        产物已经付费生成,下载抖动必须优先重试同一个签名地址,而不是重新
        提交一次付费生成任务。
        """
        item = self._generated_item("term-1", "https://cdn.example.com/1.mp4")

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                return_value=[item],
            ) as generate,
            patch(
                "app.services.material.save_video",
                side_effect=[
                    requests.exceptions.ConnectionError("boom"),
                    "/tmp/1.mp4",
                ],
            ) as save,
            patch("app.services.material.time.sleep"),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-download-retry",
                search_terms=["term-1"],
                source="wavespeed",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(result, ["/tmp/1.mp4"])
        # 重试打在同一个地址上,且没有触发第二次付费生成
        self.assertEqual(save.call_count, 2)
        self.assertEqual(generate.call_count, 1)
        for call in save.call_args_list:
            self.assertEqual(
                call.kwargs.get("video_url") or call.args[0],
                "https://cdn.example.com/1.mp4",
            )

    def test_download_videos_wavespeed_bypasses_search_cache(self):
        """
        生成源不参与 24 小时搜索缓存:签名 URL 会过期,复用缓存还会让不同
        任务反复拿到同一段生成视频。download_videos 必须直接调用生成函数。
        """
        generated_item = material.MaterialInfo()
        generated_item.provider = "wavespeed"
        generated_item.url = "https://cdn.example.com/out.mp4?sig=abc"
        generated_item.duration = 5
        generated_item.source_info = {
            "provider": "wavespeed",
            "search_term": "sunrise",
            "asset_id": "pred-123",
        }

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                return_value=[generated_item],
            ) as generate,
            patch("app.services.material._search_videos_with_cache") as cached_search,
            patch(
                "app.services.material.save_video",
                return_value="/tmp/wavespeed-saved.mp4",
            ) as save,
        ):
            result = material.download_videos(
                task_id="test-wavespeed",
                search_terms=["sunrise"],
                source="wavespeed",
                audio_duration=5,
                max_clip_duration=5,
            )

        self.assertEqual(generate.call_count, 1)
        cached_search.assert_not_called()
        save_url = save.call_args.kwargs.get("video_url") or save.call_args.args[0]
        self.assertEqual(save_url, "https://cdn.example.com/out.mp4?sig=abc")
        self.assertEqual(result, ["/tmp/wavespeed-saved.mp4"])

    def test_generate_wavespeed_clamps_duration_to_model_minimum(self):
        """
        WebUI 默认片段时长 3 秒,而默认模型只接受 4-15 秒;直接透传会被 API
        拒绝。请求必须收敛到模型下限,多出的时长由现有剪辑流程按片段时长裁掉。
        """
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-c1"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-c1",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/c1.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=3)

        self.assertEqual(post.call_args.kwargs["json"]["duration"], 4)
        # MaterialInfo 记录实际生成时长,时长核算和剪辑按真实素材长度进行
        self.assertEqual(results[0].duration, 4)

    def test_generate_wavespeed_clamps_duration_to_model_maximum(self):
        """超过模型上限的请求收敛到上限,不能提交必然失败的远端请求。"""
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-c2"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-c2",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/c2.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            results = material.generate_videos_wavespeed("sunrise", minimum_duration=20)

        self.assertEqual(post.call_args.kwargs["json"]["duration"], 15)
        self.assertEqual(results[0].duration, 15)

    def test_generate_wavespeed_duration_bounds_are_configurable(self):
        """切换到其它模型时,用户可以在配置中同步调整支持的时长区间。"""
        config.app["wavespeed_min_duration"] = 2
        config.app["wavespeed_max_duration"] = 8
        submit_response = self._json_response({"code": 200, "data": {"id": "pred-c3"}})
        poll_response = self._json_response(
            {
                "code": 200,
                "data": {
                    "id": "pred-c3",
                    "status": "completed",
                    "outputs": ["https://cdn.example.com/c3.mp4"],
                },
            }
        )

        with (
            patch(
                "app.services.material.requests.post", return_value=submit_response
            ) as post,
            patch("app.services.material.requests.get", return_value=poll_response),
        ):
            material.generate_videos_wavespeed("sunrise", minimum_duration=3)

        self.assertEqual(post.call_args.kwargs["json"]["duration"], 3)

    @staticmethod
    def _generated_item(term, url, duration=5):
        item = material.MaterialInfo()
        item.provider = "wavespeed"
        item.url = url
        item.duration = duration
        item.source_info = {
            "provider": "wavespeed",
            "search_term": term,
            "asset_id": f"pred-{term}",
        }
        return item

    def test_download_videos_wavespeed_generates_on_demand_and_stops(self):
        """
        生成按条计费,不能先为全部关键词生成再挑选。素材必须逐段按需生成,
        累计有效时长(按片段时长封顶)超过所需配音时长后,后续关键词不再
        触发任何生成请求。
        """
        generated = {
            "term-1": [self._generated_item("term-1", "https://cdn.example.com/1.mp4")],
            "term-2": [self._generated_item("term-2", "https://cdn.example.com/2.mp4")],
            "term-3": [self._generated_item("term-3", "https://cdn.example.com/3.mp4")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material.save_video",
                side_effect=lambda video_url, save_dir="": f"/tmp/{video_url.rsplit('/', 1)[-1]}",
            ),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-lazy",
                search_terms=["term-1", "term-2", "term-3"],
                source="wavespeed",
                audio_duration=8,
                max_clip_duration=5,
            )

        # 5s + 5s > 8s,第三个关键词不能再产生付费生成请求
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            [call.kwargs["search_term"] for call in generate.call_args_list],
            ["term-1", "term-2"],
        )
        self.assertEqual(result, ["/tmp/1.mp4", "/tmp/2.mp4"])

    def test_download_videos_wavespeed_stops_when_duration_exactly_covered(self):
        """
        边界回归:配音 10 秒、每段 5 秒时,累计恰好等于所需时长即已够用,
        第 3 个关键词不能再触发付费生成请求(停止判断必须是 >= 而不是 >)。
        """
        generated = {
            "term-1": [self._generated_item("term-1", "https://cdn.example.com/1.mp4")],
            "term-2": [self._generated_item("term-2", "https://cdn.example.com/2.mp4")],
            "term-3": [self._generated_item("term-3", "https://cdn.example.com/3.mp4")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material.save_video",
                side_effect=lambda video_url, save_dir="": f"/tmp/{video_url.rsplit('/', 1)[-1]}",
            ),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-exact",
                search_terms=["term-1", "term-2", "term-3"],
                source="wavespeed",
                audio_duration=10,
                max_clip_duration=5,
            )

        # 5s + 5s == 10s,恰好覆盖,第 3 段绝不能生成
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result, ["/tmp/1.mp4", "/tmp/2.mp4"])

    def test_download_videos_wavespeed_skips_failed_segment_and_continues(self):
        """单个片段生成失败(空结果)时跳过该关键词,继续为后续片段生成。"""
        generated = {
            "term-1": [],
            "term-2": [self._generated_item("term-2", "https://cdn.example.com/2.mp4")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_videos_wavespeed",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material.save_video",
                return_value="/tmp/wavespeed-2.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-wavespeed-skip",
                search_terms=["term-1", "term-2"],
                source="wavespeed",
                audio_duration=4,
                max_clip_duration=5,
            )

        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result, ["/tmp/wavespeed-2.mp4"])


class TestMaterialDownloadProgress(unittest.TestCase):
    """
    素材下载是任务里第二长的阶段，进度回调必须真实推进且不能拖垮下载。
    """

    def _run_download(self, progress_callback, match_script_order=False):
        items = [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/{i}.mp4",
                duration=5,
                source_info={"provider": "pexels", "asset_id": i},
            )
            for i in range(1, 5)
        ]

        def fake_search(search_term, minimum_duration, video_aspect):
            return items

        with (
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(
                material,
                "save_video",
                side_effect=lambda video_url, save_dir="": f"/tmp/{video_url[-5:]}",
            ),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts, "patch_script_data", return_value=True
            ),
        ):
            return material.download_videos(
                task_id="progress-materials",
                search_terms=["city"],
                source="pexels",
                audio_duration=10,
                max_clip_duration=5,
                match_script_order=match_script_order,
                progress_callback=progress_callback,
            )

    def test_download_reports_increasing_fractions_capped_at_one(self):
        """
        进度必须单调递增并且封顶 1.0：下载会在覆盖旁白时长后立即停止，
        最后一段常常超出所需时长，未封顶就会把上层进度推过分段上限。
        """
        seen = []
        self._run_download(lambda fraction, _paths: seen.append(fraction))

        self.assertTrue(seen, "download reported no progress at all")
        self.assertEqual(seen, sorted(seen))
        self.assertLessEqual(max(seen), 1.0)
        self.assertGreater(max(seen), 0.0)

    def test_script_order_download_also_reports_progress(self):
        """
        顺序匹配是另一条下载路径，同样会跑好几分钟，不能只有默认路径有进度。
        """
        seen = []
        self._run_download(
            lambda fraction, _paths: seen.append(fraction), match_script_order=True
        )

        self.assertTrue(seen, "script-order download reported no progress")
        self.assertLessEqual(max(seen), 1.0)

    def test_each_clip_is_reported_as_soon_as_it_is_saved(self):
        """
        下载要跑几分钟。第一段落盘后就应该能被上层拿到并播放，而不是等整批
        下载完；否则用户对着进度条干等，素材其实早就在磁盘上了。
        """
        reports = []
        result = self._run_download(
            lambda _fraction, paths: reports.append(paths)
        )

        self.assertEqual(len(reports), len(result))
        self.assertEqual(len(reports[0]), 1, "the first clip must be reported alone")
        self.assertEqual([len(paths) for paths in reports], list(range(1, len(result) + 1)))
        self.assertEqual(reports[-1], result)

    def test_a_reported_clip_list_is_a_snapshot(self):
        """
        回调收到的必须是副本。传引用的话，接收方存下来的“当时的列表”会随着
        后续下载继续变化，界面上就会出现还没下载完就已经列出的片段。
        """
        reports = []
        self._run_download(lambda _fraction, paths: reports.append(paths))

        self.assertEqual(len(reports[0]), 1, "the first snapshot grew after the fact")
        self.assertNotEqual(reports[0], reports[-1])

    def test_review_flow_overrides_the_configured_material_directory(self):
        """
        审阅流程必须把素材下载到任务目录，无视 material_directory 配置。

        既有安装会保留创建时的配置值，改默认值不会影响它们；而 /stream/ 只
        服务任务目录，配置一旦不是 "task"，审阅界面就会全部没有预览。
        """
        saved_dirs = []

        def fake_search(search_term, minimum_duration, video_aspect):
            return [
                material.MaterialInfo(
                    provider="pexels",
                    url="https://v.example/1.mp4",
                    duration=30,
                    source_info={"provider": "pexels", "asset_id": 1},
                )
            ]

        def fake_save_video(video_url, save_dir=""):
            saved_dirs.append(save_dir)
            return "/tmp/clip.mp4"

        with (
            # The configured value is deliberately the one that breaks previews.
            patch.dict(config.app, {"material_directory": ""}),
            patch.object(material, "search_videos_pexels", side_effect=fake_search),
            patch.object(material, "save_video", side_effect=fake_save_video),
            patch.object(
                material.material_cache,
                "load_material_search_cache",
                return_value=None,
            ),
            patch.object(material.material_cache, "save_material_search_cache"),
            patch.object(
                material.task_artifacts, "patch_script_data", return_value=True
            ),
        ):
            material.download_videos(
                task_id="review-task",
                search_terms=["city"],
                source="pexels",
                audio_duration=5,
                max_clip_duration=5,
                force_material_directory="/tasks/review-task",
            )

        self.assertEqual(saved_dirs, ["/tasks/review-task"])

    def test_failing_progress_callback_does_not_abort_download(self):
        """
        回调只负责上报。它抛异常时必须被吞掉，否则一次状态写入失败就会
        丢掉已经下载成功的素材，让整个任务白跑。
        """

        def exploding(_fraction, _paths):
            raise RuntimeError("state backend unavailable")

        result = self._run_download(exploding)

        self.assertTrue(result, "a failing callback must not lose downloaded videos")


class TestKeywordReplacementSearch(unittest.TestCase):
    """
    审阅界面按关键词换片段：必须跳过时间线已有的候选，并且只下载一段。

    重复点击"再找一个"要给出不同的结果，否则这个功能等于没有；而搜索结果
    有 24 小时缓存，所以"下一个"只能由调用方传来的排除列表决定。
    """

    def _items(self, count=3):
        return [
            material.MaterialInfo(
                provider="pexels",
                url=f"https://v.example/{index}.mp4",
                duration=10,
                source_info={"provider": "pexels", "asset_id": str(index)},
            )
            for index in range(1, count + 1)
        ]

    def _search(self, *, items, exclude=(), source="pexels", save_dir="/tmp/task"):
        saved = []

        def fake_save(video_url, save_dir=""):
            path = os.path.join(save_dir, material.downloaded_file_name(video_url))
            saved.append(path)
            return path

        with (
            patch.object(
                material, "_search_videos_with_cache", return_value=list(items)
            ) as search,
            patch.object(material, "save_video", side_effect=fake_save),
            patch.object(material, "_append_material_source") as append_source,
        ):
            result = material.search_replacement_video(
                task_id="task-123",
                search_term="city skyline",
                source=source,
                exclude_paths=list(exclude),
                save_dir=save_dir,
            )
        return result, saved, search, append_source

    def test_returns_the_first_candidate_and_downloads_only_it(self):
        """换一段素材只该产生一次下载，不能顺带把整批候选都取回来。"""
        items = self._items()
        result, saved, _search, _append = self._search(items=items)

        self.assertEqual(
            result.path,
            os.path.join("/tmp/task", material.downloaded_file_name(items[0].url)),
        )
        self.assertEqual(result.provider, "pexels")
        self.assertEqual(len(saved), 1)

    def test_skips_clips_already_in_the_timeline(self):
        """
        已经在时间线上的片段必须在下载之前就跳过。

        排除是按 save_video 会用的文件名做的，所以重复候选不会先花一次
        下载再被发现——那既浪费带宽，也会把用户的配额用在已有的素材上。
        """
        items = self._items()
        taken = os.path.join(
            "/tmp/task", material.downloaded_file_name(items[0].url)
        )
        result, saved, _search, _append = self._search(items=items, exclude=[taken])

        self.assertEqual(
            result.path,
            os.path.join("/tmp/task", material.downloaded_file_name(items[1].url)),
        )
        self.assertEqual(saved, [result.path], "the excluded candidate was downloaded")

    def test_reports_nothing_when_every_candidate_is_already_used(self):
        """候选全部用过时返回空串，由调用方提示换个关键词，而不是重复给同一段。"""
        items = self._items(2)
        taken = [
            os.path.join("/tmp/task", material.downloaded_file_name(item.url))
            for item in items
        ]
        result, saved, _search, _append = self._search(items=items, exclude=taken)

        self.assertEqual(result.path, "")
        self.assertEqual(saved, [])

    def test_blank_keyword_never_reaches_the_provider(self):
        """空关键词不该产生一次远端搜索。"""
        with patch.object(material, "_search_videos_with_cache") as search:
            result = material.search_replacement_video(
                task_id="task-123", search_term="   ", save_dir="/tmp/task"
            )

        self.assertEqual(result.path, "")
        search.assert_not_called()

    def test_local_and_unknown_sources_fall_back_to_a_stock_provider(self):
        """
        本地素材项目没有可搜索的目录，但"在自己的片段中间插一段库存素材"
        是完全正常的诉求，所以这里回退到默认库存源而不是直接失败。
        """
        items = self._items(1)
        _result, _saved, search, _append = self._search(items=items, source="local")

        self.assertEqual(
            search.call_args.kwargs["provider"], material.DEFAULT_SEARCHABLE_SOURCE
        )

    def test_an_exhausted_provider_falls_through_to_another_one(self):
        """
        一个关键词在单一供应商上很快就见底：搜索结果缓存一天，方向筛选又会
        再砍一批。见底就报"没有了"会让"换一个"两三次之后就失效，所以要顺延
        到其它已配置 Key 的库存源。
        """
        fresh = material.MaterialInfo(
            provider="pixabay",
            url="https://v.example/fresh.mp4",
            duration=10,
            source_info={"provider": "pixabay"},
        )
        used = self._items(1)
        taken = [
            os.path.join("/tmp/task", material.downloaded_file_name(used[0].url))
        ]

        def by_provider(**kwargs):
            return used if kwargs["provider"] == "pexels" else [fresh]

        with (
            patch.object(
                material, "_search_videos_with_cache", side_effect=by_provider
            ),
            patch.object(
                material,
                "save_video",
                side_effect=lambda video_url, save_dir="": os.path.join(
                    save_dir, material.downloaded_file_name(video_url)
                ),
            ),
            patch.object(material, "_append_material_source"),
        ):
            result = material.search_replacement_video(
                task_id="task-123",
                search_term="city skyline",
                source="pexels",
                exclude_paths=taken,
                save_dir="/tmp/task",
            )

        self.assertEqual(
            result.path,
            os.path.join("/tmp/task", material.downloaded_file_name(fresh.url)),
        )
        self.assertEqual(
            result.provider, "pixabay", "the answer must name who supplied it"
        )

    def test_a_fallback_without_a_key_is_skipped_in_silence(self):
        """
        备用源没配 Key 不是用户的问题——他们选的是另一个源。这里必须继续往下
        找，而不是把一条关于他们从没选过的服务的报错丢到界面上。
        """
        fresh = material.MaterialInfo(
            provider="coverr", url="https://v.example/c.mp4", duration=10
        )

        def by_provider(**kwargs):
            if kwargs["provider"] == "pexels":
                return []
            if kwargs["provider"] == "pixabay":
                raise ValueError("pixabay_api_keys is not set")
            return [fresh]

        with (
            patch.object(
                material, "_search_videos_with_cache", side_effect=by_provider
            ),
            patch.object(
                material,
                "save_video",
                side_effect=lambda video_url, save_dir="": os.path.join(
                    save_dir, material.downloaded_file_name(video_url)
                ),
            ),
            patch.object(material, "_append_material_source"),
        ):
            result = material.search_replacement_video(
                task_id="task-123",
                search_term="city skyline",
                source="pexels",
                save_dir="/tmp/task",
            )

        self.assertEqual(result.provider, "coverr")

    def test_the_requested_provider_still_reports_a_missing_key(self):
        """
        用户选定的源缺 Key 必须说出来：那是他们能修的，静默顺延会让"为什么
        Pexels 的 Key 白填了"变成一个查不出来的问题。
        """
        with patch.object(
            material,
            "_search_videos_with_cache",
            side_effect=ValueError("pexels_api_keys is not set"),
        ):
            with self.assertRaises(ValueError):
                material.search_replacement_video(
                    task_id="task-123",
                    search_term="city skyline",
                    source="pexels",
                    save_dir="/tmp/task",
                )

    def test_source_record_is_appended_not_replaced(self):
        """
        单段替换必须保留其它片段的来源记录。整体替换会把它们抹掉，而来源
        信息是素材授权追溯的依据。
        """
        _result, _saved, _search, append_source = self._search(items=self._items(1))

        append_source.assert_called_once()
        self.assertEqual(append_source.call_args.args[0], "task-123")


class TestAppendMaterialSource(unittest.TestCase):
    """来源记录是追加写入：单段替换不能覆盖整批素材的授权信息。"""

    def test_existing_records_survive_a_single_replacement(self):
        existing = [{"provider": "pexels", "file": "vid-1.mp4"}]
        added = {"provider": "pexels", "file": "vid-2.mp4"}

        with (
            patch.object(
                material.task_artifacts,
                "read_script_data",
                return_value={"material_sources": list(existing)},
            ),
            patch.object(
                material.task_artifacts, "patch_script_data", return_value=True
            ) as patch_data,
        ):
            material._append_material_source("task-123", added)

        self.assertEqual(
            patch_data.call_args.kwargs["material_sources"], existing + [added]
        )

    def test_a_missing_manifest_still_records_the_new_clip(self):
        """任务清单不存在时也不能抛异常：片段已经下载好了。"""
        with (
            patch.object(
                material.task_artifacts, "read_script_data", return_value=None
            ),
            patch.object(
                material.task_artifacts, "patch_script_data", return_value=False
            ) as patch_data,
        ):
            material._append_material_source("task-123", {"provider": "pexels"})

        self.assertEqual(
            patch_data.call_args.kwargs["material_sources"], [{"provider": "pexels"}]
        )


if __name__ == "__main__":
    unittest.main()
