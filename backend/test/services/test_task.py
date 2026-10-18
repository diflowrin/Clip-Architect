import unittest
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import task as tm
from app.models.schema import MaterialInfo, VideoAspect, VideoParams
from app.services.state import MemoryState, RedisState
from app.utils import utils

resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources")
RUN_INTEGRATION_TESTS = os.environ.get("MPT_RUN_INTEGRATION_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
}


class TestTaskService(unittest.TestCase):
    def setUp(self):
        # 发布 Future 注册表是进程级状态。测试间清理可以避免某个模拟 Future
        # 影响后续恢复测试，同时不会触碰真正线程池中的生产任务。
        with tm._cross_post_registry_lock:
            tm._cross_post_futures.clear()

    def tearDown(self):
        with tm._cross_post_registry_lock:
            tm._cross_post_futures.clear()

    def test_is_task_busy_covers_generation_and_cross_posting(self):
        """删除入口必须同时识别视频生成和跨平台发布的活跃状态。"""
        busy_tasks = (
            {"state": tm.const.TASK_STATE_PROCESSING},
            {
                "state": tm.const.TASK_STATE_COMPLETE,
                "cross_post_state": tm.const.CROSS_POST_STATE_PENDING,
            },
            {
                "state": tm.const.TASK_STATE_COMPLETE,
                "cross_post_state": tm.const.CROSS_POST_STATE_PROCESSING,
            },
        )
        for task in busy_tasks:
            with self.subTest(task=task):
                self.assertTrue(tm.is_task_busy(task))

        self.assertFalse(
            tm.is_task_busy(
                {
                    "state": tm.const.TASK_STATE_COMPLETE,
                    "cross_post_state": tm.const.CROSS_POST_STATE_COMPLETE,
                }
            )
        )
        self.assertFalse(tm.is_task_busy(None))

    def test_generate_script_forwards_advanced_prompt_options(self):
        """
        任务生成入口和 WebUI/API 共用 VideoParams。这里验证自动生成文案时，
        高级提示词参数会继续传到 LLM 服务层，避免只在 /scripts 接口生效。
        """
        params = VideoParams(
            video_subject="咖啡",
            video_script="",
            video_language="zh-CN",
            paragraph_number=2,
            video_duration=40,
            voice_rate=1.0,
            video_script_prompt="语气轻松",
            custom_system_prompt="Only write short narration.",
        )

        with patch.object(
            tm.llm, "generate_script", return_value="生成的文案"
        ) as generate:
            result = tm.generate_script("task-id", params)

        self.assertEqual(result, "生成的文案")
        generate.assert_called_once_with(
            video_subject="咖啡",
            language="zh-CN",
            paragraph_number=2,
            video_script_prompt="语气轻松",
            custom_system_prompt="Only write short narration.",
            # 用户选的片长只能在这里生效：文案一旦写完，旁白多长就已经定了。
            target_seconds=40,
            words_per_second=tm.llm.NARRATION_WORDS_PER_SECOND,
            video_aspect=params.video_aspect,
        )

    def test_the_shape_of_the_video_reaches_the_script_prompt(self):
        """
        A vertical clip is watched in a feed the viewer can swipe away and a
        widescreen one is not, and the aspect is the only thing on the form that
        says which this is.
        """
        params = VideoParams(
            video_subject="coffee",
            video_script="",
            video_aspect=VideoAspect.landscape,
        )

        with patch.object(tm.llm, "generate_script", return_value="script") as generate:
            tm.generate_script("task-id", params)

        self.assertEqual(
            generate.call_args.kwargs["video_aspect"], VideoAspect.landscape
        )

    def test_script_word_budget_follows_the_voice_rate(self):
        """语速更快时，同样的秒数能读完更多词，预算必须跟着变。"""
        params = VideoParams(
            video_subject="coffee",
            video_script="",
            video_duration=30,
            voice_rate=1.5,
        )

        with patch.object(tm.llm, "generate_script", return_value="script") as generate:
            tm.generate_script("task-id", params)

        self.assertAlmostEqual(
            generate.call_args.kwargs["words_per_second"],
            tm.llm.NARRATION_WORDS_PER_SECOND * 1.5,
        )

    def test_generate_final_videos_forwards_clip_speed(self):
        """任务编排层必须把用户选择的画面速度传给视频合成服务。"""
        params = VideoParams(
            video_subject="test",
            video_count=1,
            video_clip_speed=1.25,
        )

        with (
            patch.object(tm.video, "combine_videos") as combine_videos,
            patch.object(tm.video, "generate_video"),
            patch.object(tm.sm.state, "update_task"),
        ):
            tm.generate_final_videos(
                task_id="clip-speed-task",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        self.assertEqual(combine_videos.call_args.kwargs["clip_speed"], 1.25)

    def test_render_stages_report_what_they_are_doing(self):
        """
        拼接和烧字幕是整条流水线里最慢的两段，各自会占住进度条好几分钟。
        它们各自的回调必须同时写进度和当前动作，否则界面只剩一个不动的数字。
        """
        params = VideoParams(video_subject="test", video_count=1)

        def fake_combine(**kwargs):
            kwargs["progress_callback"](0.5)

        def fake_generate(**kwargs):
            kwargs["progress_callback"](0.5)
            return True

        with (
            patch.object(tm.video, "combine_videos", side_effect=fake_combine),
            patch.object(tm.video, "generate_video", side_effect=fake_generate),
            patch.object(tm.sm.state, "update_task") as update_task,
        ):
            tm.generate_final_videos(
                task_id="stage-message-render",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        messages = [
            call.kwargs.get("stage_message") for call in update_task.call_args_list
        ]
        self.assertTrue(messages, "the render loop reported no progress")
        self.assertTrue(
            all(message for message in messages),
            "a render progress write carried no stage message",
        )
        self.assertIn("Assembling the video", messages)
        self.assertIn("Adding subtitles and audio", messages)

    def test_render_stage_messages_count_multiple_videos(self):
        """生成多条视频时，文案要说明正在处理第几条，否则进度看起来在打转。"""
        params = VideoParams(video_subject="test", video_count=2)

        def fake_combine(**kwargs):
            kwargs["progress_callback"](1.0)

        def fake_generate(**kwargs):
            kwargs["progress_callback"](1.0)
            return True

        with (
            patch.object(tm.video, "combine_videos", side_effect=fake_combine),
            patch.object(tm.video, "generate_video", side_effect=fake_generate),
            patch.object(tm.sm.state, "update_task") as update_task,
        ):
            tm.generate_final_videos(
                task_id="stage-message-multi",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        messages = [
            call.kwargs.get("stage_message") for call in update_task.call_args_list
        ]
        self.assertIn("Assembling the video (1 of 2)", messages)
        self.assertIn("Assembling the video (2 of 2)", messages)

    def test_generate_final_videos_keeps_explicit_sequential_order(self):
        """
        素材审阅界面允许用户手工排列片段，这个顺序只有 sequential 才会保留。
        多视频生成的默认打散不能覆盖用户显式选择，否则排好的顺序会被静默
        丢弃；未显式选择时仍应保持原有的多视频打散行为。
        """
        for concat_mode, expected in (
            ("sequential", tm.VideoConcatMode.sequential),
            ("random", tm.VideoConcatMode.random),
        ):
            with self.subTest(concat_mode=concat_mode):
                params = VideoParams(
                    video_subject="test",
                    video_count=2,
                    video_concat_mode=concat_mode,
                )

                with (
                    patch.object(tm.video, "combine_videos") as combine_videos,
                    patch.object(tm.video, "generate_video"),
                    patch.object(tm.sm.state, "update_task"),
                ):
                    tm.generate_final_videos(
                        task_id="concat-order-task",
                        params=params,
                        downloaded_videos=["material.mp4"],
                        audio_file="audio.mp3",
                        subtitle_path="",
                        audio_duration=5,
                    )

                self.assertEqual(
                    combine_videos.call_args.kwargs["video_concat_mode"],
                    expected,
                )

    def test_generate_final_videos_uses_generated_sonilo_music(self):
        """Sonilo 必须针对每条拼接后的视频生成配乐，并传给最终混音。"""
        params = VideoParams(
            video_subject="test",
            video_count=1,
            bgm_type="sonilo",
            sonilo_bgm_prompt="warm acoustic",
        )

        with (
            patch.object(tm.video, "combine_videos"),
            patch.object(
                tm.sonilo,
                "generate_bgm",
                side_effect=lambda **kwargs: kwargs["output_path"],
            ) as generate_bgm,
            patch.object(tm.video, "generate_video") as generate_video,
            patch.object(tm.sm.state, "update_task"),
        ):
            _, _, warnings = tm.generate_final_videos(
                task_id="sonilo-task",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        self.assertEqual(warnings, [])
        self.assertEqual(generate_bgm.call_args.kwargs["video_duration"], 5)
        self.assertEqual(generate_bgm.call_args.kwargs["prompt"], "warm acoustic")
        self.assertTrue(
            generate_video.call_args.kwargs["bgm_file_override"].endswith(
                "sonilo-bgm-1.m4a"
            )
        )

    def test_generate_final_videos_uses_generated_elevenlabs_music(self):
        """ElevenLabs 应复用视频配乐编排，并使用通用风格提示词。"""
        params = VideoParams(
            video_subject="test",
            video_count=1,
            bgm_type="elevenlabs",
            video_music_prompt="gentle documentary",
        )

        with (
            patch.object(tm.video, "combine_videos"),
            patch.object(
                tm.elevenlabs_music,
                "generate_bgm",
                side_effect=lambda **kwargs: kwargs["output_path"],
            ) as generate_bgm,
            patch.object(tm.video, "generate_video") as generate_video,
            patch.object(tm.sm.state, "update_task"),
        ):
            _, _, warnings = tm.generate_final_videos(
                task_id="elevenlabs-task",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        self.assertEqual(warnings, [])
        self.assertEqual(generate_bgm.call_args.kwargs["video_duration"], 5)
        self.assertEqual(generate_bgm.call_args.kwargs["prompt"], "gentle documentary")
        self.assertTrue(
            generate_video.call_args.kwargs["bgm_file_override"].endswith(
                "elevenlabs-bgm-1.mp3"
            )
        )

    def test_generate_final_videos_falls_back_on_elevenlabs_failure(self):
        """ElevenLabs 暂时失败时必须保留无配乐视频和结构化警告。"""
        params = VideoParams(video_subject="test", bgm_type="elevenlabs")

        with (
            patch.object(tm.video, "combine_videos"),
            patch.object(
                tm.elevenlabs_music,
                "generate_bgm",
                side_effect=tm.elevenlabs_music.ElevenLabsMusicError(
                    "temporary outage"
                ),
            ),
            patch.object(tm.video, "generate_video") as generate_video,
            patch.object(tm.sm.state, "update_task"),
        ):
            final_paths, _, warnings = tm.generate_final_videos(
                task_id="elevenlabs-fallback",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        self.assertEqual(len(final_paths), 1)
        self.assertEqual(
            warnings,
            [{"code": "elevenlabs_bgm_failed", "video_index": 1}],
        )
        self.assertEqual(generate_video.call_args.kwargs["bgm_file_override"], "")

    def test_generate_final_videos_falls_back_without_bgm_on_sonilo_failure(self):
        """第三方配乐失败时应完成视频并返回可见警告，而不是丢弃所有产物。"""
        params = VideoParams(video_subject="test", bgm_type="sonilo")

        with (
            patch.object(tm.video, "combine_videos"),
            patch.object(
                tm.sonilo,
                "generate_bgm",
                side_effect=tm.sonilo.SoniloError("temporary outage"),
            ),
            patch.object(tm.video, "generate_video") as generate_video,
            patch.object(tm.sm.state, "update_task"),
        ):
            final_paths, _, warnings = tm.generate_final_videos(
                task_id="sonilo-fallback",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        self.assertEqual(len(final_paths), 1)
        self.assertEqual(warnings, [{"code": "sonilo_bgm_failed", "video_index": 1}])
        self.assertEqual(generate_video.call_args.kwargs["bgm_file_override"], "")

    def test_generate_final_videos_skips_sonilo_when_volume_is_zero(self):
        """0 音量必须完全跳过 Sonilo 生成，并显式禁用残留背景音乐。"""
        params = VideoParams(
            video_subject="test",
            bgm_type="sonilo",
            bgm_volume=0.0,
            bgm_file="stale-custom-bgm.mp3",
        )

        with (
            patch.object(tm.video, "combine_videos"),
            patch.object(tm.sonilo, "generate_bgm") as generate_bgm,
            patch.object(tm.video, "generate_video", return_value=True) as generate,
            patch.object(tm.sm.state, "update_task"),
        ):
            final_paths, _, warnings = tm.generate_final_videos(
                task_id="sonilo-zero-volume",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        self.assertEqual(len(final_paths), 1)
        self.assertEqual(warnings, [])
        generate_bgm.assert_not_called()
        self.assertEqual(generate.call_args.kwargs["bgm_file_override"], "")

    def test_generate_final_videos_warns_when_sonilo_mix_fails(self):
        """Sonilo 生成成功但最终混音失败时，任务必须保留视频并返回警告。"""
        params = VideoParams(video_subject="test", bgm_type="sonilo")

        with (
            patch.object(tm.video, "combine_videos"),
            patch.object(
                tm.sonilo,
                "generate_bgm",
                side_effect=lambda **kwargs: kwargs["output_path"],
            ),
            patch.object(tm.video, "generate_video", return_value=False) as generate,
            patch.object(tm.sm.state, "update_task"),
        ):
            final_paths, _, warnings = tm.generate_final_videos(
                task_id="sonilo-mix-fallback",
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )

        self.assertEqual(len(final_paths), 1)
        self.assertEqual(warnings, [{"code": "sonilo_bgm_failed", "video_index": 1}])
        self.assertTrue(generate.call_args.kwargs["bgm_file_override"].endswith(".m4a"))

    def _render_with_music(self, params, task_id, task_root, songs, draws):
        """Run one render with a controlled bundled library, and report the mix."""
        empty_uploads = os.path.join(task_root, "uploads")
        os.makedirs(empty_uploads, exist_ok=True)
        with (
            patch.object(tm.utils, "task_dir", return_value=task_root),
            patch.object(tm.bgm_service.utils, "song_dir", return_value=songs),
            patch.object(tm.bgm_service, "uploaded_bgm_dir", return_value=empty_uploads),
            patch.object(tm.video, "combine_videos"),
            patch.object(tm.video, "get_bgm_file", side_effect=draws) as get_bgm_file,
            patch.object(tm.video, "generate_video") as generate_video,
            patch.object(tm.sm.state, "update_task"),
        ):
            tm.generate_final_videos(
                task_id=task_id,
                params=params,
                downloaded_videos=["material.mp4"],
                audio_file="audio.mp3",
                subtitle_path="",
                audio_duration=5,
            )
        return get_bgm_file, [
            call.kwargs["params"] for call in generate_video.call_args_list
        ]

    def test_random_music_stays_the_same_when_a_task_is_rendered_again(self):
        """
        重新渲染同一个任务不能换掉背景音乐。

        随机曲目原本在 generate_video 内部每次重抽，而"审阅后改一个地方再渲染
        一次"正是这个界面存在的理由——用户没有碰音乐，配乐却整个换了。抽签结果
        写进 script.json，下次渲染直接复用。

        用假的 get_bgm_file 每次返回不同曲目，而不是依赖真随机：真随机重抽
        也可能碰巧抽中同一首，那样的断言证明不了任何事。
        """
        with tempfile.TemporaryDirectory() as task_root:
            songs = os.path.join(task_root, "songs")
            os.makedirs(songs)
            for name in ("one.mp3", "two.mp3"):
                Path(songs, name).write_bytes(b"song")
            draws = [os.path.join(songs, "one.mp3"), os.path.join(songs, "two.mp3")]
            params = VideoParams(
                video_subject="test", video_count=1, bgm_type="random", bgm_volume=0.2
            )

            first_draw, first_params = self._render_with_music(
                params, "bgm-pin", task_root, songs, list(draws)
            )
            second_draw, second_params = self._render_with_music(
                params, "bgm-pin", task_root, songs, list(draws)
            )

        self.assertEqual(first_draw.call_count, 1)
        # The second render must not reach for the dice at all.
        self.assertEqual(second_draw.call_count, 0)
        self.assertEqual(first_params[0].bgm_type, "file")
        self.assertEqual(
            os.path.basename(first_params[0].bgm_file or ""), "one.mp3"
        )
        self.assertEqual(second_params[0].bgm_file, first_params[0].bgm_file)

    def test_choosing_a_track_and_going_back_to_random_draws_again(self):
        """
        表单换了音乐设置，钉住的曲目就该作废。

        中间那次渲染指定了具体曲目，会把 script.json 里的记录覆盖成
        "type=file"；再切回 Random 时配方不再匹配，因此必须重新抽，而不是
        复活这个任务上一次抽到的那首。
        """
        with tempfile.TemporaryDirectory() as task_root:
            songs = os.path.join(task_root, "songs")
            os.makedirs(songs)
            for name in ("one.mp3", "two.mp3", "chosen.mp3"):
                Path(songs, name).write_bytes(b"song")
            draws = [os.path.join(songs, "one.mp3"), os.path.join(songs, "two.mp3")]

            random_params = VideoParams(
                video_subject="test", video_count=1, bgm_type="random", bgm_volume=0.2
            )
            named_params = VideoParams(
                video_subject="test",
                video_count=1,
                bgm_type="file",
                bgm_file="chosen.mp3",
                bgm_volume=0.2,
            )

            self._render_with_music(
                random_params, "bgm-reroll", task_root, songs, list(draws)
            )
            _, named_render = self._render_with_music(
                named_params, "bgm-reroll", task_root, songs, list(draws)
            )
            second_draw, back_to_random = self._render_with_music(
                random_params, "bgm-reroll", task_root, songs, list(draws)
            )

        # A named track is passed through untouched - it needs no pin.
        self.assertEqual(named_render[0].bgm_type, "file")
        self.assertEqual(named_render[0].bgm_file, "chosen.mp3")
        self.assertEqual(second_draw.call_count, 1)
        self.assertEqual(
            os.path.basename(back_to_random[0].bgm_file or ""), "one.mp3"
        )

    def test_each_video_of_a_batch_keeps_its_own_track(self):
        """
        video_count 大于 1 时每条视频本来就各抽各的，钉住不能把它们并成一首。
        重新渲染时也要按顺序各自复用。
        """
        with tempfile.TemporaryDirectory() as task_root:
            songs = os.path.join(task_root, "songs")
            os.makedirs(songs)
            for name in ("one.mp3", "two.mp3", "three.mp3"):
                Path(songs, name).write_bytes(b"song")
            draws = [
                os.path.join(songs, "one.mp3"),
                os.path.join(songs, "two.mp3"),
                os.path.join(songs, "three.mp3"),
            ]
            params = VideoParams(
                video_subject="test", video_count=2, bgm_type="random", bgm_volume=0.2
            )

            first_draw, first_params = self._render_with_music(
                params, "bgm-batch", task_root, songs, list(draws)
            )
            second_draw, second_params = self._render_with_music(
                params, "bgm-batch", task_root, songs, list(draws)
            )

        self.assertEqual(first_draw.call_count, 2)
        self.assertEqual(
            [os.path.basename(entry.bgm_file or "") for entry in first_params],
            ["one.mp3", "two.mp3"],
        )
        self.assertEqual(second_draw.call_count, 0)
        self.assertEqual(
            [entry.bgm_file for entry in second_params],
            [entry.bgm_file for entry in first_params],
        )

    def test_run_pipeline_fails_fast_when_ffmpeg_is_not_ready(self):
        """完整视频流水线必须在 LLM/TTS/素材服务之前先确认 FFmpeg 可用。"""
        params = VideoParams(video_subject="test")
        state = MemoryState()
        with (
            patch.object(tm.utils, "check_ffmpeg_ready", return_value=False),
            patch.object(tm, "generate_script") as generate_script,
            patch.object(tm, "generate_audio") as generate_audio,
            patch.object(tm, "get_video_materials") as get_materials,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("ffmpeg-missing", params)

        generate_script.assert_not_called()
        generate_audio.assert_not_called()
        get_materials.assert_not_called()
        self.assertEqual(result["state"], tm.const.TASK_STATE_FAILED)
        self.assertEqual(result["failed_stage"], "preflight")
        self.assertIn("ffmpeg", result["error"])

    def test_run_pipeline_skips_ffmpeg_check_for_script_stage(self):
        """脚本阶段不涉及音频/视频合成，不应因为缺少 FFmpeg 而被拒绝。"""
        params = VideoParams(video_subject="test")
        state = MemoryState()
        with (
            patch.object(tm.utils, "check_ffmpeg_ready", return_value=False) as check,
            patch.object(tm, "generate_script", return_value="脚本") as generate_script,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("ffmpeg-missing-script-stage", params, stop_at="script")

        check.assert_not_called()
        generate_script.assert_called_once()
        self.assertEqual(result, {"script": "脚本"})

    def test_run_pipeline_skips_ffmpeg_check_for_terms_stage(self):
        """搜索词阶段同样不需要 FFmpeg，不应触发探测。"""
        params = VideoParams(video_subject="test")
        state = MemoryState()
        with (
            patch.object(tm.utils, "check_ffmpeg_ready", return_value=False) as check,
            patch.object(tm, "generate_script", return_value="脚本"),
            patch.object(tm, "generate_terms", return_value=["term"]),
            patch.object(tm, "save_script_data"),
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("ffmpeg-missing-terms-stage", params, stop_at="terms")

        check.assert_not_called()
        self.assertEqual(result, {"script": "脚本", "terms": ["term"]})

    def test_run_pipeline_proceeds_past_ffmpeg_preflight_when_ready(self):
        """FFmpeg 可用时探测不应阻塞后续脚本生成。"""
        params = VideoParams(video_subject="test")
        state = MemoryState()
        with (
            patch.object(tm.utils, "check_ffmpeg_ready", return_value=True) as check,
            patch.object(tm, "generate_script", return_value="脚本") as generate_script,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("ffmpeg-ready", params, stop_at="script")

        # 即使 script 阶段不强制要求 FFmpeg，这里也验证探测函数被跳过调用，
        # 与"仅在 script/terms 之外阶段才检查"的约定保持一致。
        check.assert_not_called()
        generate_script.assert_called_once()
        self.assertEqual(result, {"script": "脚本"})

    def test_start_rejects_missing_sonilo_key_before_costly_pipeline_steps(self):
        """完整任务缺少 Sonilo Key 时不能先调用 LLM、TTS 或素材服务。"""
        params = VideoParams(video_subject="test", bgm_type="sonilo")
        state = MemoryState()
        with (
            patch.object(tm.sonilo, "is_enabled", return_value=False),
            patch.object(tm, "generate_script") as generate_script,
            patch.object(tm, "generate_audio") as generate_audio,
            patch.object(tm, "get_video_materials") as get_materials,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("missing-sonilo-key", params)

        generate_script.assert_not_called()
        generate_audio.assert_not_called()
        get_materials.assert_not_called()
        failed_task = state.get_task("missing-sonilo-key")
        self.assertEqual(result, failed_task)
        self.assertEqual(failed_task["state"], tm.const.TASK_STATE_FAILED)
        self.assertEqual(failed_task["failed_stage"], "preflight")
        self.assertIn("API key", failed_task["error"])

    def test_start_does_not_require_sonilo_key_when_volume_is_zero(self):
        """0 音量不会使用 Sonilo，因此缺少 Key 时仍应进入正常任务流水线。"""
        params = VideoParams(
            video_subject="test",
            bgm_type="sonilo",
            bgm_volume=0.0,
        )
        state = MemoryState()
        with (
            patch.object(tm.sonilo, "is_enabled", return_value=False),
            patch.object(tm, "generate_script", return_value="") as generate_script,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("zero-volume-without-key", params)

        generate_script.assert_called_once_with("zero-volume-without-key", params)
        self.assertEqual(result["failed_stage"], "script")

    def test_loomloom_material_failure_keeps_remote_run_id(self):
        """远端运行已创建后失败，任务状态必须保留 LoomLoom run ID。"""
        params = VideoParams(video_subject="AI 办公", video_source="loomloom")
        settings = tm.loomloom.LoomLoomSettings(
            base_url="https://example.test/loom/v1",
            api_token="test-token",
            market_listing_id=tm.loomloom.DEFAULT_SCRIPT_MARKET_LISTING_ID,
        )
        batch = tm.loomloom.LoomLoomVideoBatch(
            input_rows=(
                {
                    "scenePrompt": "office worker",
                    "aspectRatio": "9:16",
                    "sceneIndex": "1",
                },
            ),
        )
        request = tm.loomloom.LoomLoomConfirmedVideoRequest(
            settings=settings,
            batch=batch,
            listing_version_id="version-1",
            client_request_id="mpt-video-1",
        )
        backend = MagicMock()
        backend.execute.return_value = tm.loomloom.LoomLoomExecution(
            run_id="run-1",
            transaction_id="transaction-1",
            transaction_status="running",
            listing_version_id="version-1",
        )
        backend.wait_for_run.side_effect = tm.loomloom.LoomLoomRunError(
            "remote run timeout"
        )
        state = MemoryState()
        state.update_task(
            "loomloom-material-timeout",
            state=tm.const.TASK_STATE_PROCESSING,
            progress=40,
        )

        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm.loomloom,
                "LoomLoomVideoBackend",
                return_value=backend,
            ),
        ):
            result = tm.get_video_materials(
                "loomloom-material-timeout",
                params,
                ["office worker"],
                10,
                loomloom_video_request=request,
            )

        self.assertIsNone(result)
        failed_task = state.get_task("loomloom-material-timeout")
        self.assertEqual(failed_task["state"], tm.const.TASK_STATE_FAILED)
        self.assertEqual(failed_task["failed_stage"], "materials")
        self.assertEqual(failed_task["loomloom_run_id"], "run-1")
        self.assertEqual(failed_task["loomloom_listing_version_id"], "version-1")

    def test_loomloom_state_failure_does_not_abandon_paid_remote_run(self):
        """状态后端不可用时仍需等待并下载已经开始计费的远端任务。"""
        params = VideoParams(video_subject="AI 办公", video_source="loomloom")
        settings = tm.loomloom.LoomLoomSettings(
            base_url="https://example.test/loom/v1",
            api_token="test-token",
            market_listing_id=tm.loomloom.DEFAULT_VIDEO_MARKET_LISTING_ID,
        )
        request = tm.loomloom.LoomLoomConfirmedVideoRequest(
            settings=settings,
            batch=tm.loomloom.LoomLoomVideoBatch(
                input_rows=(
                    {
                        "scenePrompt": "office worker",
                        "aspectRatio": "9:16",
                        "sceneIndex": "1",
                    },
                )
            ),
            listing_version_id="version-1",
            client_request_id="mpt-video-state-failure",
        )
        backend = MagicMock()
        backend.execute.return_value = tm.loomloom.LoomLoomExecution(
            run_id="paid-run-1",
            transaction_id="transaction-1",
            transaction_status="running",
            listing_version_id="version-1",
        )
        backend.download_video_results.return_value = ("clip.mp4",)
        unavailable_state = MagicMock()
        unavailable_state.patch_task.side_effect = RuntimeError("Redis unavailable")

        with (
            patch.object(tm.sm, "state", unavailable_state),
            patch.object(
                tm.loomloom,
                "LoomLoomVideoBackend",
                return_value=backend,
            ),
            patch.object(tm.time, "sleep") as sleep,
        ):
            result = tm.get_video_materials(
                "loomloom-state-failure",
                params,
                ["office worker"],
                10,
                loomloom_video_request=request,
            )

        self.assertEqual(result, ["clip.mp4"])
        self.assertEqual(
            unavailable_state.patch_task.call_count,
            tm._LOOMLOOM_STATE_WRITE_ATTEMPTS,
        )
        self.assertEqual(
            sleep.call_count,
            tm._LOOMLOOM_STATE_WRITE_ATTEMPTS - 1,
        )
        backend.wait_for_run.assert_called_once_with("paid-run-1")
        backend.download_video_results.assert_called_once()

    def test_mark_task_failed_preserves_a_specific_service_failure(self):
        """服务层已记录具体错误时，编排层不能再用通用错误覆盖它。"""
        state = MemoryState()
        state.update_task(
            "specific-service-failure",
            state=tm.const.TASK_STATE_FAILED,
            progress=40,
            failed_stage="materials",
            error="remote run timed out",
            loomloom_run_id="run-1",
        )

        with patch.object(tm.sm, "state", state):
            result = tm._mark_task_failed(
                "specific-service-failure",
                "materials",
                "failed to prepare video materials",
            )

        self.assertEqual(result["error"], "remote run timed out")
        self.assertEqual(result["loomloom_run_id"], "run-1")

    def test_start_rejects_missing_elevenlabs_key_before_pipeline_steps(self):
        """完整任务缺少 ElevenLabs Key 时必须在任何付费步骤前失败。"""
        params = VideoParams(video_subject="test", bgm_type="elevenlabs")
        state = MemoryState()
        with (
            patch.object(tm.elevenlabs_music, "is_enabled", return_value=False),
            patch.object(tm, "generate_script") as generate_script,
            patch.object(tm, "generate_audio") as generate_audio,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("missing-elevenlabs-key", params)

        generate_script.assert_not_called()
        generate_audio.assert_not_called()
        self.assertEqual(result["state"], tm.const.TASK_STATE_FAILED)
        self.assertEqual(result["failed_stage"], "preflight")
        self.assertIn("ElevenLabs", result["error"])

    def test_start_rejects_free_elevenlabs_plan_before_pipeline_steps(self):
        """已确认的免费套餐不能先消耗 LLM、TTS 或素材服务额度。"""
        params = VideoParams(video_subject="test", bgm_type="elevenlabs")
        state = MemoryState()
        with (
            patch.object(tm.elevenlabs_music, "is_enabled", return_value=True),
            patch.object(
                tm.elevenlabs_music,
                "validate_generation_access",
                side_effect=(
                    tm.elevenlabs_music.ElevenLabsPaidPlanRequiredError(
                        "ElevenLabs Music API requires a paid plan"
                    )
                ),
            ) as validate_access,
            patch.object(tm, "generate_script") as generate_script,
            patch.object(tm, "generate_audio") as generate_audio,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("free-elevenlabs-plan", params)

        validate_access.assert_called_once_with()
        generate_script.assert_not_called()
        generate_audio.assert_not_called()
        self.assertEqual(result["failed_stage"], "preflight")
        self.assertIn("paid plan", result["error"])

    def test_start_rejects_oversized_elevenlabs_prompt_before_account_check(self):
        """API/CLI 绕过 WebUI 时，超长提示词也必须在昂贵步骤前被拒绝。"""
        params = VideoParams(
            video_subject="test",
            bgm_type="elevenlabs",
            video_music_prompt="x" * 1001,
        )
        state = MemoryState()
        with (
            patch.object(tm.elevenlabs_music, "is_enabled", return_value=True),
            patch.object(
                tm.elevenlabs_music, "validate_generation_access"
            ) as validate_access,
            patch.object(tm, "generate_script") as generate_script,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("oversized-elevenlabs-prompt", params)

        validate_access.assert_not_called()
        generate_script.assert_not_called()
        self.assertEqual(result["failed_stage"], "preflight")
        self.assertIn("1000", result["error"])

    def test_generate_terms_uses_script_order_mode_when_enabled(self):
        """
        默认模式不受影响；只有用户显式开启素材按文案顺序匹配时，任务层才
        要求 LLM 生成有序关键词，并适当增加关键词数量以覆盖更多脚本片段。
        """
        params = VideoParams(
            video_subject="城市通勤",
            video_script="",
            match_materials_to_script=True,
        )

        with patch.object(
            tm.llm, "generate_terms", return_value=["city", "train"]
        ) as generate:
            result = tm.generate_terms("task-id", params, "先城市，再地铁")

        self.assertEqual(result, ["city", "train"])
        generate.assert_called_once_with(
            video_subject="城市通勤",
            video_script="先城市，再地铁",
            amount=8,
            match_script_order=True,
        )

    def test_start_stops_before_materials_when_term_provider_fails(self):
        """
        关键词 Provider 失败后，任务必须立即结束，不能继续生成音频或下载素材。

        这里从任务入口覆盖完整的错误传播路径，避免未来只修服务层返回类型，
        却又在任务编排层把空列表转换成其它真值后继续执行外部请求。
        """
        params = VideoParams(
            video_subject="startup story",
            video_script="A short startup story.",
        )
        state = MemoryState()

        with (
            patch.object(
                tm.llm,
                "_generate_response",
                return_value="Error: invalid API key",
            ),
            patch.object(tm, "generate_audio") as generate_audio,
            patch.object(tm, "get_video_materials") as get_video_materials,
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("term-provider-error", params)

        generate_audio.assert_not_called()
        get_video_materials.assert_not_called()
        failed_task = state.get_task("term-provider-error")
        self.assertEqual(result, failed_task)
        self.assertEqual(failed_task["state"], tm.const.TASK_STATE_FAILED)
        self.assertEqual(failed_task["failed_stage"], "terms")
        self.assertTrue(failed_task["error"])

    def test_recording_the_narration_alone_records_how_long_it_runs(self):
        """
        单独跑配音就是为了知道成片会有多长。时长只作为返回值给了调用方，
        而调用方是轮询任务状态的前端——那个答案存在，却没人读得到。
        """
        params = VideoParams(video_subject="a subject", video_script="a script")
        writes = []

        with (
            patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(tm, "generate_script", return_value="a script"),
            patch.object(tm, "generate_terms", return_value=["term"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm, "generate_audio", return_value=("audio.mp3", 42, object())
            ),
            patch.object(
                tm.sm.state,
                "update_task",
                side_effect=lambda task_id, **fields: writes.append(fields),
            ),
        ):
            result = tm.start("audio-only", params, stop_at="audio")

        self.assertEqual(result, {"audio_file": "audio.mp3", "audio_duration": 42})
        completed = writes[-1]
        self.assertEqual(completed["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(completed["audio_file"], "audio.mp3")
        self.assertEqual(completed["audio_duration"], 42)

    def test_a_paid_voice_with_no_key_says_which_key_is_missing(self):
        """
        缺凭据和服务不可达在任务里是同一个 None。界面上只剩一句"合成失败"，
        用户要么去查网络，要么去改一个本来就没问题的设置。
        """
        task_id = "test-missing-tts-credential"
        task_dir = utils.task_dir(task_id)
        params = VideoParams(
            video_subject="a subject", voice_name="elevenlabs:abc:Adam"
        )
        state = MemoryState()

        try:
            with (
                patch.object(tm.voice, "tts", return_value=None),
                patch.object(
                    tm.voice,
                    "missing_credentials",
                    return_value="the ElevenLabs API key is not set in Settings",
                ),
                patch.object(tm.sm, "state", state),
            ):
                result = tm.generate_audio(task_id, params, "script")
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(result, (None, None, None))
        self.assertIn("ElevenLabs API key", state.get_task(task_id)["error"])

    def test_a_free_voice_that_fails_keeps_the_general_message(self):
        """Edge 不需要任何凭据，失败时能给的只有"检查音色和网络"。"""
        task_id = "test-generic-tts-failure"
        task_dir = utils.task_dir(task_id)
        params = VideoParams(
            video_subject="a subject", voice_name="en-US-AriaNeural-Female"
        )
        state = MemoryState()

        try:
            with (
                patch.object(tm.voice, "tts", return_value=None),
                patch.object(tm.voice, "missing_credentials", return_value=""),
                patch.object(tm.sm, "state", state),
            ):
                tm.generate_audio(task_id, params, "script")
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertIn("connectivity", state.get_task(task_id)["error"])

    def test_generate_audio_uses_custom_file_inside_task_directory(self):
        task_id = "test-custom-audio-safe"
        task_dir = utils.task_dir(task_id)
        custom_audio_file = os.path.join(task_dir, "custom-audio.mp3")
        with open(custom_audio_file, "wb") as audio:
            audio.write(b"fake audio")

        params = VideoParams(
            video_subject="custom audio",
            video_script="",
            custom_audio_file=custom_audio_file,
            voice_name="test-voice",
        )

        try:
            with (
                patch.object(tm.voice, "tts") as tts,
                patch.object(tm.voice, "get_audio_duration", return_value=7),
            ):
                audio_file, audio_duration, sub_maker = tm.generate_audio(
                    task_id, params, "script"
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(audio_file, os.path.realpath(custom_audio_file))
        self.assertEqual(audio_duration, 7)
        self.assertIsNone(sub_maker)
        tts.assert_not_called()

    def test_generate_audio_rejects_server_side_custom_file_by_default(self):
        task_id = "test-custom-audio-untrusted-server-side"
        task_dir = utils.task_dir(task_id)
        state = MemoryState()

        with tempfile.NamedTemporaryFile(suffix=".mp3") as server_audio:
            server_audio.write(b"fake audio")
            server_audio.flush()
            params = VideoParams(
                video_subject="custom audio",
                video_script="",
                custom_audio_file=server_audio.name,
                voice_name="test-voice",
            )

            try:
                with (
                    patch.object(tm.voice, "tts") as tts,
                    patch.object(tm.voice, "get_audio_duration") as get_duration,
                    patch.object(tm.sm, "state", state),
                ):
                    audio_file, audio_duration, result_sub_maker = tm.generate_audio(
                        task_id, params, "script"
                    )
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)

        self.assertIsNone(audio_file)
        self.assertIsNone(audio_duration)
        self.assertIsNone(result_sub_maker)
        tts.assert_not_called()
        get_duration.assert_not_called()
        failed_task = state.get_task(task_id)
        self.assertEqual(failed_task["failed_stage"], "audio")
        self.assertIn("current task directory", failed_task["error"])

    def test_external_custom_audio_error_does_not_reveal_file_existence(self):
        task_id = "test-custom-audio-existence-oracle"
        task_dir = utils.task_dir(task_id)

        with tempfile.NamedTemporaryFile(suffix=".mp3") as server_audio:
            external_paths = [server_audio.name, f"{server_audio.name}.missing"]
            errors = []
            try:
                for external_path in external_paths:
                    with self.assertRaises(ValueError) as raised:
                        tm.resolve_custom_audio_file(task_id, external_path)
                    errors.append(str(raised.exception))
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(errors[0], errors[1])
        self.assertIn("current task directory", errors[0])

    def test_generate_audio_accepts_server_side_custom_file_for_trusted_cli(self):
        task_id = "test-custom-audio-server-side"
        task_dir = utils.task_dir(task_id)

        with tempfile.NamedTemporaryFile(suffix=".mp3") as server_audio:
            server_audio.write(b"fake audio")
            server_audio.flush()
            params = VideoParams(
                video_subject="custom audio",
                video_script="",
                custom_audio_file=server_audio.name,
                voice_name="test-voice",
            )

            try:
                with (
                    patch.object(tm.voice, "tts") as tts,
                    patch.object(tm.voice, "get_audio_duration", return_value=6),
                ):
                    audio_file, audio_duration, result_sub_maker = tm.generate_audio(
                        task_id,
                        params,
                        "script",
                        allow_server_file_input=True,
                    )
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(audio_file, os.path.realpath(server_audio.name))
        self.assertEqual(audio_duration, 6)
        self.assertIsNone(result_sub_maker)
        tts.assert_not_called()

    def test_generate_audio_rejects_missing_custom_file_without_tts(self):
        task_id = "test-custom-audio-missing"
        task_dir = utils.task_dir(task_id)
        missing_audio_file = os.path.join(task_dir, "missing.mp3")
        params = VideoParams(
            video_subject="custom audio",
            video_script="",
            custom_audio_file=missing_audio_file,
            voice_name="test-voice",
        )
        state = MemoryState()

        try:
            with (
                patch.object(tm.voice, "tts") as tts,
                patch.object(tm.sm, "state", state),
            ):
                audio_file, audio_duration, result_sub_maker = tm.generate_audio(
                    task_id, params, "script"
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertIsNone(audio_file)
        self.assertIsNone(audio_duration)
        self.assertIsNone(result_sub_maker)
        tts.assert_not_called()
        failed_task = state.get_task(task_id)
        self.assertEqual(failed_task["failed_stage"], "audio")
        self.assertIn("does not exist", failed_task["error"])

    def test_generate_subtitle_uses_whisper_for_custom_audio_without_sub_maker(self):
        """
        自定义音频不会经过 TTS，所以没有 sub_maker。
        Whisper 可以直接从音频文件转写，此时不能被 sub_maker 为空的保护逻辑提前跳过。
        """
        task_id = "test-custom-audio-whisper-subtitle"
        task_dir = utils.task_dir(task_id)
        audio_file = os.path.join(task_dir, "custom-audio.mp3")
        Path(audio_file).write_bytes(b"fake audio")
        params = VideoParams(
            video_subject="custom audio",
            video_script="Hello world.",
            subtitle_enabled=True,
        )

        def fake_whisper_create(audio_file, subtitle_file):
            Path(subtitle_file).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello world.\n\n",
                encoding="utf-8",
            )

        try:
            with (
                patch.object(
                    tm.config,
                    "app",
                    dict(tm.config.app, subtitle_provider="whisper"),
                ),
                patch.object(
                    tm.subtitle, "create", side_effect=fake_whisper_create
                ) as create,
                patch.object(tm.subtitle, "correct") as correct,
            ):
                subtitle_path = tm.generate_subtitle(
                    task_id=task_id,
                    params=params,
                    video_script="Hello world.",
                    sub_maker=None,
                    audio_file=audio_file,
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertTrue(subtitle_path.endswith("subtitle.srt"))
        create.assert_called_once_with(
            audio_file=audio_file, subtitle_file=subtitle_path
        )
        correct.assert_called_once_with(
            subtitle_file=subtitle_path, video_script="Hello world."
        )

    def test_generate_subtitle_skips_edge_provider_without_sub_maker(self):
        """
        Edge 字幕依赖 TTS 返回的 sub_maker 时间轴。
        自定义音频缺少该对象时应继续跳过，避免产生不可信的字幕时间轴。
        """
        task_id = "test-custom-audio-edge-no-submaker"
        task_dir = utils.task_dir(task_id)
        audio_file = os.path.join(task_dir, "custom-audio.mp3")
        Path(audio_file).write_bytes(b"fake audio")
        params = VideoParams(
            video_subject="custom audio",
            video_script="Hello world.",
            subtitle_enabled=True,
        )

        try:
            with (
                patch.object(
                    tm.config,
                    "app",
                    dict(tm.config.app, subtitle_provider="edge"),
                ),
                patch.object(tm.voice, "create_subtitle") as create_subtitle,
                patch.object(tm.subtitle, "create") as whisper_create,
            ):
                subtitle_path = tm.generate_subtitle(
                    task_id=task_id,
                    params=params,
                    video_script="Hello world.",
                    sub_maker=None,
                    audio_file=audio_file,
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(subtitle_path, "")
        create_subtitle.assert_not_called()
        whisper_create.assert_not_called()

    def test_generate_subtitle_does_not_fallback_to_whisper_when_edge_fails(self):
        """
        Edge 没有生成字幕文件时应保留无字幕结果，不能自动下载 Whisper 模型。

        该场景可能由 TTS 时间轴与原始文案无法匹配触发。自动回退会让未选择
        Whisper 的用户意外下载数 GB 模型，因此必须验证 Whisper 完全不会被调用。
        """
        task_id = "test-edge-subtitle-without-output"
        task_dir = utils.task_dir(task_id)
        params = VideoParams(
            video_subject="edge subtitle",
            video_script="Hello world.",
            subtitle_enabled=True,
        )
        sub_maker = object()

        try:
            with (
                patch.object(
                    tm.config,
                    "app",
                    dict(tm.config.app, subtitle_provider="edge"),
                ),
                patch.object(tm.voice, "create_subtitle") as create_subtitle,
                patch.object(tm.subtitle, "create") as whisper_create,
                patch.object(tm.subtitle, "correct") as whisper_correct,
            ):
                subtitle_path = tm.generate_subtitle(
                    task_id=task_id,
                    params=params,
                    video_script="Hello world.",
                    sub_maker=sub_maker,
                    audio_file=os.path.join(task_dir, "audio.mp3"),
                )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(subtitle_path, "")
        create_subtitle.assert_called_once()
        whisper_create.assert_not_called()
        whisper_correct.assert_not_called()

    def test_start_returns_each_intermediate_result(self):
        """
        API 的 script、terms、audio、subtitle 和 materials 模式共用同一条任务
        流水线。每个提前停止点都要返回对应产物，同时不能误执行后续阶段。
        """
        expected_results = {
            "script": {"script": "generated script"},
            "terms": {
                "script": "generated script",
                "terms": ["coffee", "morning"],
            },
            "audio": {"audio_file": "audio.mp3", "audio_duration": 5},
            "subtitle": {"subtitle_path": "subtitle.srt"},
            "materials": {"materials": ["clip.mp4"]},
        }

        for stop_at, expected in expected_results.items():
            with self.subTest(stop_at=stop_at):
                params = VideoParams(video_subject="Coffee")
                with (
                    patch.object(
                        tm, "generate_script", return_value="generated script"
                    ),
                    patch.object(
                        tm,
                        "generate_terms",
                        return_value=["coffee", "morning"],
                    ),
                    patch.object(tm, "save_script_data"),
                    patch.object(
                        tm,
                        "generate_audio",
                        return_value=("audio.mp3", 5, object()),
                    ),
                    patch.object(
                        tm,
                        "generate_subtitle",
                        return_value="subtitle.srt",
                    ),
                    patch.object(
                        tm,
                        "get_video_materials",
                        return_value=["clip.mp4"],
                    ),
                    patch.object(tm, "generate_final_videos") as generate_final,
                    patch.object(tm.sm.state, "update_task"),
                ):
                    result = tm.start(
                        f"intermediate-{stop_at}", params, stop_at=stop_at
                    )

                self.assertEqual(result, expected)
                generate_final.assert_not_called()

    def test_local_footage_can_stop_for_review_like_a_stock_search(self):
        """
        自己上传的素材同样要能进审阅编辑器：排序、删除、替换都发生在那里，
        而传错一条片子的代价和搜错素材一样，都是一次完整渲染。
        """
        params = VideoParams(video_subject="My trip")
        params.video_source = "local"
        params.video_materials = [
            MaterialInfo(provider="local", url="clip.mp4", duration=0)
        ]
        clips = ["/storage/local_videos/clip.mp4"]
        writes = []

        with (
            patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(tm, "generate_script", return_value="a narration"),
            patch.object(tm, "generate_terms") as generate_terms,
            patch.object(tm, "save_script_data"),
            patch.object(
                tm, "generate_audio", return_value=("audio.mp3", 5, object())
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(tm, "get_video_materials", return_value=clips),
            patch.object(tm, "generate_final_videos") as generate_final,
            patch.object(
                tm.sm.state,
                "update_task",
                side_effect=lambda task_id, **fields: writes.append(fields),
            ),
        ):
            result = tm.start("local-review", params, stop_at="materials")

        self.assertEqual(result, {"materials": clips})
        generate_final.assert_not_called()
        # Keywords exist to search stock libraries with. There is nothing to
        # search here, and generating them would bill an LLM call for nothing.
        generate_terms.assert_not_called()

        completed = writes[-1]
        self.assertEqual(completed["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(completed["materials"], clips)
        # The reviewed render reuses every one of these instead of running the
        # pipeline again. A stop that dropped one would fail the render that
        # follows on a missing narration, or ship it with no subtitles.
        self.assertEqual(completed["script"], "a narration")
        self.assertEqual(completed["audio_file"], "audio.mp3")
        self.assertEqual(completed["audio_duration"], 5)
        self.assertEqual(completed["subtitle_path"], "subtitle.srt")

    def test_a_video_without_narration_skips_the_voice_and_the_subtitles(self):
        """
        无旁白模式只想要"文案 → 关键词 → 素材"这条链路。跑 TTS 会付一次没人
        听的钱，而字幕时间轴本来就来自 TTS，没有旁白就没有字幕可言。
        """
        params = VideoParams(video_subject="Wild animals")
        params.voice_enabled = False
        params.video_duration = 45
        writes = []

        with (
            patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(tm, "generate_script", return_value="a narration"),
            patch.object(tm, "generate_terms", return_value=["lion", "savanna"]),
            patch.object(tm, "save_script_data"),
            patch.object(tm, "generate_audio") as generate_audio,
            patch.object(tm, "generate_subtitle") as generate_subtitle,
            patch.object(
                tm, "get_video_materials", return_value=["clip.mp4"]
            ) as get_materials,
            patch.object(tm, "generate_final_videos") as generate_final,
            patch.object(
                tm.sm.state,
                "update_task",
                side_effect=lambda task_id, **fields: writes.append(fields),
            ),
        ):
            tm.start("silent-1", params, stop_at="materials")

        generate_audio.assert_not_called()
        generate_subtitle.assert_not_called()
        generate_final.assert_not_called()
        # Keywords are the whole point of the script here: they are what the
        # footage provider is asked for.
        self.assertEqual(get_materials.call_args.args[2], ["lion", "savanna"])
        # And the requested length is what says how much footage to fetch.
        self.assertEqual(get_materials.call_args.args[3], 45)

        completed = writes[-1]
        self.assertEqual(completed["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(completed["audio_file"], "")
        self.assertEqual(completed["subtitle_path"], "")
        self.assertEqual(completed["audio_duration"], 45)

    def test_the_audio_and_subtitle_endpoints_ignore_the_narration_switch(self):
        """
        /audio 和 /subtitle 存在的意义就是产出这两个文件。它们的请求模型里
        没有这个开关，读到一个继承来的 False 就什么都不生成，是纯粹的倒退。
        """
        params = VideoParams(video_subject="Wild animals")
        params.voice_enabled = False

        for stop_at in ("audio", "subtitle"):
            with self.subTest(stop_at=stop_at):
                with (
                    patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
                    patch.object(tm, "generate_script", return_value="a narration"),
                    patch.object(tm, "generate_terms", return_value=["lion"]),
                    patch.object(tm, "save_script_data"),
                    patch.object(
                        tm,
                        "generate_audio",
                        return_value=("audio.mp3", 5, object()),
                    ) as generate_audio,
                    patch.object(
                        tm, "generate_subtitle", return_value="subtitle.srt"
                    ),
                    patch.object(tm.sm.state, "update_task"),
                ):
                    tm.start(f"forced-{stop_at}", params, stop_at=stop_at)

                generate_audio.assert_called_once()

    def test_start_forwards_trusted_server_file_flag_to_audio_stage(self):
        params = VideoParams(video_subject="CLI custom audio")

        with (
            patch.object(tm.utils, "check_ffmpeg_ready", return_value=True),
            patch.object(tm, "generate_script", return_value="generated script"),
            patch.object(tm, "generate_terms", return_value=["audio"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm,
                "generate_audio",
                return_value=("audio.mp3", 5, None),
            ) as generate_audio,
            patch.object(tm.sm.state, "update_task"),
        ):
            result = tm.start(
                "trusted-cli-audio",
                params,
                stop_at="audio",
                allow_server_file_input=True,
            )

        self.assertEqual(result, {"audio_file": "audio.mp3", "audio_duration": 5})
        generate_audio.assert_called_once_with(
            "trusted-cli-audio",
            params,
            "generated script",
            voice_preview=None,
            allow_server_file_input=True,
        )

    def test_start_completes_video_without_cross_posting(self):
        """
        完整任务在自动发布未配置时仍应稳定完成，并把所有中间产物写入最终
        状态。这里还覆盖 API 可能传入字符串拼接模式的兼容转换。
        """
        params = VideoParams(video_subject="Coffee")
        params.video_concat_mode = "sequential"

        with (
            patch.object(tm, "generate_script", return_value="generated script"),
            patch.object(tm, "generate_terms", return_value=["coffee"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm,
                "generate_audio",
                return_value=("audio.mp3", 5, object()),
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(
                tm,
                "get_video_materials",
                return_value=["clip.mp4"],
            ),
            patch.object(
                tm,
                "generate_final_videos",
                return_value=(["final.mp4"], ["combined.mp4"], []),
            ),
            patch.object(
                tm.upload_post.upload_post_service,
                "is_configured",
                return_value=False,
            ),
            patch.object(tm.upload_post, "cross_post_video") as cross_post,
            patch.object(tm.sm.state, "update_task") as update_task,
        ):
            result = tm.start("complete-video", params)

        self.assertEqual(result["videos"], ["final.mp4"])
        self.assertEqual(result["combined_videos"], ["combined.mp4"])
        self.assertEqual(result["cross_post_results"], None)
        self.assertEqual(params.video_concat_mode, tm.VideoConcatMode.sequential)
        cross_post.assert_not_called()
        update_task.assert_called_with(
            "complete-video",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            **result,
        )

    def test_start_marks_pipeline_failures(self):
        """
        音频、素材和最终视频任一关键产物缺失时都必须进入失败状态，不能把
        不完整任务误报为完成。三个场景复用相同 mock，仅替换故障阶段。
        """
        failure_cases = {
            "audio": (
                (None, None, None),
                ["clip.mp4"],
                (["final.mp4"], ["combined.mp4"], []),
            ),
            "materials": (
                ("audio.mp3", 5, object()),
                None,
                (["final.mp4"], ["combined.mp4"], []),
            ),
            "video": (("audio.mp3", 5, object()), ["clip.mp4"], ([], [], [])),
        }

        for stage, failure_results in failure_cases.items():
            with self.subTest(stage=stage):
                audio_result, materials_result, videos_result = failure_results
                params = VideoParams(video_subject="Coffee")
                state = MemoryState()
                with (
                    patch.object(
                        tm, "generate_script", return_value="generated script"
                    ),
                    patch.object(tm, "generate_terms", return_value=["coffee"]),
                    patch.object(tm, "save_script_data"),
                    patch.object(tm, "generate_audio", return_value=audio_result),
                    patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
                    patch.object(
                        tm,
                        "get_video_materials",
                        return_value=materials_result,
                    ),
                    patch.object(
                        tm,
                        "generate_final_videos",
                        return_value=videos_result,
                    ),
                    patch.object(tm.sm, "state", state),
                ):
                    result = tm.start(f"failed-{stage}", params)

                failed_task = state.get_task(f"failed-{stage}")
                self.assertEqual(result, failed_task)
                self.assertEqual(failed_task["state"], tm.const.TASK_STATE_FAILED)
                self.assertEqual(failed_task["failed_stage"], stage)
                self.assertTrue(failed_task["error"])

    def test_start_records_unexpected_pipeline_exception(self):
        """未预期异常也必须结束任务，并向 API 暴露原始异常类型和信息。"""
        params = VideoParams(video_subject="Coffee")
        state = MemoryState()

        with (
            patch.object(
                tm,
                "generate_script",
                side_effect=RuntimeError("provider connection reset"),
            ),
            patch.object(tm.sm, "state", state),
        ):
            result = tm.start("unexpected-failure", params)

        failed_task = state.get_task("unexpected-failure")
        self.assertEqual(result, failed_task)
        self.assertEqual(failed_task["state"], tm.const.TASK_STATE_FAILED)
        self.assertEqual(failed_task["failed_stage"], "pipeline")
        self.assertEqual(
            failed_task["error"],
            "RuntimeError: provider connection reset",
        )

    def test_start_generates_youtube_metadata_for_each_cross_post(self):
        """
        自动发布到 YouTube 时只生成一次元数据，但要把同一份字段传给每个
        成片，并在任务结果中保留每次上传成功或失败的独立结果。
        """
        params = VideoParams(
            video_subject="Coffee",
            video_language="en",
        )
        metadata = {
            "title": "Morning Coffee",
            "caption": "A better morning.",
            "hashtags": ["coffee", "shorts"],
        }
        service = tm.upload_post.upload_post_service
        state = MemoryState()

        def run_immediately(function, *args):
            future = Future()
            try:
                function(*args)
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(None)
            return future

        with (
            patch.object(tm, "generate_script", return_value="generated script"),
            patch.object(tm, "generate_terms", return_value=["coffee"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm,
                "generate_audio",
                return_value=("audio.mp3", 5, object()),
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(
                tm,
                "get_video_materials",
                return_value=["clip.mp4"],
            ),
            patch.object(
                tm,
                "generate_final_videos",
                return_value=(
                    ["final-1.mp4", "final-2.mp4"],
                    ["combined-1.mp4", "combined-2.mp4"],
                    [],
                ),
            ),
            patch.object(service, "is_configured", return_value=True),
            patch.object(service, "auto_upload", True),
            patch.object(service, "platforms", ["youtube"]),
            patch.object(service, "youtube_privacy_status", "unlisted"),
            patch.object(
                tm.llm,
                "generate_social_metadata",
                return_value=metadata,
            ) as generate_metadata,
            patch.object(
                tm.upload_post,
                "cross_post_video",
                side_effect=[
                    {"success": True},
                    {"success": False, "error": "upload failed"},
                ],
            ) as cross_post,
            patch.object(tm.sm, "state", state),
            patch.object(
                tm._cross_post_executor,
                "submit",
                side_effect=run_immediately,
            ),
        ):
            result = tm.start("youtube-cross-post", params)

        generate_metadata.assert_called_once_with(
            video_subject="Coffee",
            video_script="generated script",
            language="en",
            platform="youtube_shorts",
        )
        expected_extra = {
            "youtube_title": "Morning Coffee",
            "youtube_description": "A better morning.",
            "tags": ["coffee", "shorts"],
            "privacyStatus": "unlisted",
            "containsSyntheticMedia": True,
        }
        self.assertEqual(cross_post.call_count, 2)
        for call in cross_post.call_args_list:
            self.assertEqual(call.kwargs["youtube_extra"], expected_extra)
            self.assertEqual(call.kwargs["platforms"], ["youtube"])

        # start() 返回的是视频完成时的稳定快照；后台发布结果通过任务查询获取。
        self.assertEqual(result["cross_post_state"], tm.const.CROSS_POST_STATE_PENDING)
        self.assertIsNone(result["cross_post_results"])
        published_task = state.get_task("youtube-cross-post")
        self.assertEqual(published_task["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(
            published_task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED
        )
        self.assertEqual(
            published_task["cross_post_results"],
            [
                {"success": True},
                {"success": False, "error": "upload failed"},
            ],
        )
        self.assertEqual(published_task["cross_post_error"], "upload failed")

    def test_start_returns_before_cross_post_worker_runs(self):
        """视频任务完成时只提交发布工作，不能在生成线程中同步上传。"""
        params = VideoParams(video_subject="Coffee")
        service = tm.upload_post.upload_post_service
        state = MemoryState()
        submitted = []

        def capture_submission(function, *args):
            submitted.append((function, args))
            return MagicMock(spec=Future)

        with (
            patch.object(tm, "generate_script", return_value="generated script"),
            patch.object(tm, "generate_terms", return_value=["coffee"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm,
                "generate_audio",
                return_value=("audio.mp3", 5, object()),
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(tm, "get_video_materials", return_value=["clip.mp4"]),
            patch.object(
                tm,
                "generate_final_videos",
                return_value=(["final.mp4"], ["combined.mp4"], []),
            ),
            patch.object(service, "is_configured", return_value=True),
            patch.object(service, "auto_upload", True),
            patch.object(service, "platforms", ["tiktok"]),
            patch.object(service, "youtube_privacy_status", "private"),
            patch.object(tm.upload_post, "cross_post_video") as cross_post,
            patch.object(tm.sm, "state", state),
            patch.object(
                tm._cross_post_executor,
                "submit",
                side_effect=capture_submission,
            ) as submit,
        ):
            result = tm.start("deferred-cross-post", params)

        submit.assert_called_once()
        cross_post.assert_not_called()
        self.assertEqual(result["videos"], ["final.mp4"])
        self.assertEqual(result["cross_post_state"], tm.const.CROSS_POST_STATE_PENDING)
        completed_task = state.get_task("deferred-cross-post")
        self.assertEqual(completed_task["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(completed_task["progress"], 100)

        worker, worker_args = submitted[0]
        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm.upload_post,
                "cross_post_video",
                return_value={"success": True, "request_id": "upload-1"},
            ),
        ):
            worker(*worker_args)

        published_task = state.get_task("deferred-cross-post")
        self.assertEqual(published_task["videos"], ["final.mp4"])
        self.assertEqual(
            published_task["cross_post_state"], tm.const.CROSS_POST_STATE_COMPLETE
        )

    def test_cross_post_worker_failure_does_not_change_video_completion(self):
        """发布线程异常只能更新发布状态，不能破坏已完成的视频结果。"""
        state = MemoryState()
        state.update_task(
            "cross-post-worker-failure",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            videos=["final.mp4"],
            cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
        )

        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm.llm,
                "generate_social_metadata",
                side_effect=RuntimeError("metadata provider unavailable"),
            ),
            patch.object(tm.upload_post, "cross_post_video") as cross_post,
        ):
            tm._run_cross_post(
                "cross-post-worker-failure",
                ("final.mp4",),
                "Coffee",
                "A short coffee story.",
                "en",
                ("youtube",),
                "private",
            )

        cross_post.assert_not_called()
        task = state.get_task("cross-post-worker-failure")
        self.assertEqual(task["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(task["videos"], ["final.mp4"])
        self.assertEqual(task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED)
        self.assertIn("metadata provider unavailable", task["cross_post_error"])

    def test_start_returns_cross_post_scheduling_failure(self):
        """同步调度失败必须同时体现在任务状态和 start() 返回快照中。"""
        params = VideoParams(video_subject="Coffee")
        service = tm.upload_post.upload_post_service
        state = MemoryState()

        with (
            patch.object(tm, "generate_script", return_value="generated script"),
            patch.object(tm, "generate_terms", return_value=["coffee"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm,
                "generate_audio",
                return_value=("audio.mp3", 5, object()),
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(tm, "get_video_materials", return_value=["clip.mp4"]),
            patch.object(
                tm,
                "generate_final_videos",
                return_value=(["final.mp4"], ["combined.mp4"], []),
            ),
            patch.object(service, "is_configured", return_value=True),
            patch.object(service, "auto_upload", True),
            patch.object(service, "platforms", ["tiktok"]),
            patch.object(service, "youtube_privacy_status", "private"),
            patch.object(tm.sm, "state", state),
            patch.object(tm._cross_post_slots, "acquire", return_value=False),
            patch.object(tm._cross_post_executor, "submit") as submit,
        ):
            result = tm.start("cross-post-queue-full-result", params)

        submit.assert_not_called()
        self.assertEqual(result["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED)
        self.assertIn("queue is full", result["cross_post_error"])
        persisted_task = state.get_task("cross-post-queue-full-result")
        self.assertEqual(
            persisted_task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED
        )
        self.assertEqual(
            persisted_task["cross_post_error"],
            result["cross_post_error"],
        )

    def test_cross_post_schedule_failure_is_recorded_separately(self):
        """线程池拒绝新任务时应保留成片，并提供可查询的发布错误。"""
        state = MemoryState()
        slots = MagicMock()
        slots.acquire.return_value = True
        state.update_task(
            "cross-post-schedule-failure",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            videos=["final.mp4"],
            cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
        )

        with (
            patch.object(tm.sm, "state", state),
            patch.object(tm, "_cross_post_slots", slots),
            patch.object(
                tm._cross_post_executor,
                "submit",
                side_effect=RuntimeError("executor is shutting down"),
            ),
        ):
            scheduling_error = tm._schedule_cross_post(
                task_id="cross-post-schedule-failure",
                video_paths=["final.mp4"],
                params=VideoParams(video_subject="Coffee"),
                video_script="A short coffee story.",
                platforms=["tiktok"],
                youtube_privacy_status="private",
            )

        slots.release.assert_called_once_with()
        self.assertIn("executor is shutting down", scheduling_error)
        task = state.get_task("cross-post-schedule-failure")
        self.assertEqual(task["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(task["videos"], ["final.mp4"])
        self.assertEqual(task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED)
        self.assertIn("executor is shutting down", task["cross_post_error"])

    def test_cross_post_worker_always_releases_queue_slot(self):
        """发布工作异常退出时也必须归还容量，避免后续发布永久被拒绝。"""
        slots = MagicMock()
        state = MemoryState()
        state.update_task(
            "task-id",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
        )

        with (
            patch.object(tm, "_cross_post_slots", slots),
            patch.object(tm.sm, "state", state),
            patch.object(
                tm,
                "_run_cross_post",
                side_effect=RuntimeError("worker crashed"),
            ),
        ):
            tm._run_cross_post_with_slot("task-id")

        slots.release.assert_called_once_with()
        task = state.get_task("task-id")
        self.assertEqual(task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED)
        self.assertIn("worker crashed", task["cross_post_error"])

    def test_cross_post_state_backend_failure_is_logged_and_skips_upload(self):
        """首次状态写入失败时不能静默退出，也不能继续消耗发布额度。"""
        state = MagicMock()
        state.patch_task.side_effect = RuntimeError("redis unavailable")

        with (
            patch.object(tm.sm, "state", state),
            patch.object(tm.upload_post, "cross_post_video") as cross_post,
            patch.object(tm.logger, "exception") as log_exception,
            patch.object(tm.time, "sleep") as sleep,
        ):
            tm._run_cross_post(
                "state-backend-failure",
                ("final.mp4",),
                "Coffee",
                "A short coffee story.",
                "en",
                ("tiktok",),
                "private",
            )

        cross_post.assert_not_called()
        self.assertEqual(state.patch_task.call_count, 6)
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(log_exception.call_count, 2)
        self.assertTrue(
            all(
                "redis unavailable" in call.args[0]
                for call in log_exception.call_args_list
            )
        )

    def test_cross_post_state_update_retries_transient_backend_failure(self):
        """状态后端短暂失败一次后应继续发布，并最终保存完成状态。"""

        class FlakyMemoryState(MemoryState):
            def __init__(self):
                super().__init__()
                self.patch_calls = 0

            def patch_task(self, task_id, **kwargs):
                self.patch_calls += 1
                if self.patch_calls == 1:
                    raise RuntimeError("temporary redis outage")
                return super().patch_task(task_id, **kwargs)

        state = FlakyMemoryState()
        state.update_task(
            "transient-state-failure",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            videos=["final.mp4"],
            cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
        )

        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm.upload_post,
                "cross_post_video",
                return_value={"success": True, "request_id": "upload-1"},
            ) as cross_post,
            patch.object(tm.time, "sleep") as sleep,
        ):
            tm._run_cross_post(
                "transient-state-failure",
                ("final.mp4",),
                "Coffee",
                "A short coffee story.",
                "en",
                ("tiktok",),
                "private",
            )

        sleep.assert_called_once_with(tm._CROSS_POST_STATE_RETRY_DELAY_SECONDS)
        cross_post.assert_called_once()
        task = state.get_task("transient-state-failure")
        self.assertEqual(task["cross_post_state"], tm.const.CROSS_POST_STATE_COMPLETE)
        self.assertIsNone(task["cross_post_error"])

    def test_cross_post_generates_caption_for_non_youtube_platforms(self):
        """
        TikTok/Instagram 发布同样要生成一次社交文案，并把 caption 作为所有
        成片共享的发布标题，而不是直接发送原始主题。
        """
        metadata = {
            "title": "Coffee Hook",
            "caption": "Watch this coffee ritual.",
            "hashtags": ["#coffee"],
        }
        state = MemoryState()
        cases = {
            "tiktok-first": (("tiktok", "instagram"), "tiktok"),
            "instagram-first": (("instagram", "tiktok"), "instagram_reels"),
        }

        for case_name, (platforms, expected_platform) in cases.items():
            with self.subTest(case=case_name):
                task_id = f"caption-{case_name}"
                state.update_task(
                    task_id,
                    state=tm.const.TASK_STATE_COMPLETE,
                    progress=100,
                    videos=["final.mp4"],
                    cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
                )
                with (
                    patch.object(tm.sm, "state", state),
                    patch.object(
                        tm.llm,
                        "generate_social_metadata",
                        return_value=metadata,
                    ) as generate_metadata,
                    patch.object(
                        tm.upload_post,
                        "cross_post_video",
                        return_value={"success": True},
                    ) as cross_post,
                ):
                    tm._run_cross_post(
                        task_id,
                        ("final.mp4",),
                        "Coffee",
                        "A short coffee story.",
                        "en",
                        platforms,
                        "private",
                    )

                generate_metadata.assert_called_once_with(
                    video_subject="Coffee",
                    video_script="A short coffee story.",
                    language="en",
                    platform=expected_platform,
                )
                cross_post.assert_called_once()
                call = cross_post.call_args
                self.assertEqual(call.kwargs["title"], "Watch this coffee ritual.")
                self.assertEqual(call.kwargs["platforms"], list(platforms))
                self.assertIsNone(call.kwargs["youtube_extra"])
                task = state.get_task(task_id)
                self.assertEqual(
                    task["cross_post_state"], tm.const.CROSS_POST_STATE_COMPLETE
                )

    def test_cross_post_shares_metadata_between_youtube_fields_and_title(self):
        """YouTube 专属字段与共享发布标题必须来自同一次元数据调用。"""
        metadata = {
            "title": "Morning Coffee",
            "caption": "A better morning.",
            "hashtags": ["#coffee", "#shorts"],
        }
        state = MemoryState()
        state.update_task(
            "shared-youtube-metadata",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            videos=["final-1.mp4", "final-2.mp4"],
            cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
        )

        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm.llm,
                "generate_social_metadata",
                return_value=metadata,
            ) as generate_metadata,
            patch.object(
                tm.upload_post,
                "cross_post_video",
                return_value={"success": True},
            ) as cross_post,
        ):
            tm._run_cross_post(
                "shared-youtube-metadata",
                ("final-1.mp4", "final-2.mp4"),
                "Coffee",
                "A short coffee story.",
                "en",
                ("youtube",),
                "unlisted",
            )

        generate_metadata.assert_called_once_with(
            video_subject="Coffee",
            video_script="A short coffee story.",
            language="en",
            platform="youtube_shorts",
        )
        expected_extra = {
            "youtube_title": "Morning Coffee",
            "youtube_description": "A better morning.",
            "tags": ["#coffee", "#shorts"],
            "privacyStatus": "unlisted",
            "containsSyntheticMedia": True,
        }
        self.assertEqual(cross_post.call_count, 2)
        for call in cross_post.call_args_list:
            self.assertEqual(call.kwargs["title"], "A better morning.")
            self.assertEqual(call.kwargs["youtube_extra"], expected_extra)

    def test_cross_post_empty_metadata_degrades_to_fallback_title(self):
        """元数据缺失或为空时逐级退回，最终保留旧的通用兜底标题。"""
        state = MemoryState()
        cases = {
            "legacy-string": ({}, "", "Check out this video! #shorts #viral"),
            "title-over-subject": (
                {"title": "Fallback Title", "caption": ""},
                "Coffee",
                "Fallback Title",
            ),
        }

        for case_name, (metadata, subject, expected_title) in cases.items():
            with self.subTest(case=case_name):
                task_id = f"fallback-title-{case_name}"
                state.update_task(
                    task_id,
                    state=tm.const.TASK_STATE_COMPLETE,
                    progress=100,
                    videos=["final.mp4"],
                    cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
                )
                with (
                    patch.object(tm.sm, "state", state),
                    patch.object(
                        tm.llm,
                        "generate_social_metadata",
                        return_value=metadata,
                    ) as generate_metadata,
                    patch.object(
                        tm.upload_post,
                        "cross_post_video",
                        return_value={"success": True},
                    ) as cross_post,
                ):
                    tm._run_cross_post(
                        task_id,
                        ("final.mp4",),
                        subject,
                        "",
                        "",
                        ("tiktok",),
                        "private",
                    )

                generate_metadata.assert_called_once()
                cross_post.assert_called_once()
                self.assertEqual(cross_post.call_args.kwargs["title"], expected_title)

    def test_recover_interrupted_cross_posts_preserves_active_future(self):
        """启动恢复只处理遗留状态，当前进程仍持有的发布任务不能被误伤。"""
        state = MemoryState()
        for task_id in (
            "stale-pending",
            "active-processing",
            "inactive-current-owner",
            "remote-processing",
            "already-complete",
        ):
            cross_post_state = {
                "stale-pending": tm.const.CROSS_POST_STATE_PENDING,
                "active-processing": tm.const.CROSS_POST_STATE_PROCESSING,
                "inactive-current-owner": tm.const.CROSS_POST_STATE_PROCESSING,
                "remote-processing": tm.const.CROSS_POST_STATE_PROCESSING,
                "already-complete": tm.const.CROSS_POST_STATE_COMPLETE,
            }[task_id]
            state.update_task(
                task_id,
                state=tm.const.TASK_STATE_COMPLETE,
                progress=100,
                videos=["final.mp4"],
                cross_post_state=cross_post_state,
                cross_post_owner=(
                    "another-host:123:remote"
                    if task_id == "remote-processing"
                    else (
                        tm._cross_post_process_owner
                        if task_id == "inactive-current-owner"
                        else None
                    )
                ),
            )

        active_future = Future()
        tm._register_cross_post_future("active-processing", active_future)
        with patch.object(tm.sm, "state", state):
            recovered = tm.recover_interrupted_cross_posts(page_size=1)

        self.assertEqual(recovered, 2)
        stale_task = state.get_task("stale-pending")
        self.assertEqual(
            stale_task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED
        )
        self.assertEqual(
            stale_task["cross_post_error"], tm._INTERRUPTED_CROSS_POST_ERROR
        )
        self.assertEqual(
            state.get_task("active-processing")["cross_post_state"],
            tm.const.CROSS_POST_STATE_PROCESSING,
        )
        self.assertEqual(
            state.get_task("inactive-current-owner")["cross_post_state"],
            tm.const.CROSS_POST_STATE_FAILED,
        )
        self.assertEqual(
            state.get_task("remote-processing")["cross_post_state"],
            tm.const.CROSS_POST_STATE_PROCESSING,
        )
        self.assertEqual(
            state.get_task("already-complete")["cross_post_state"],
            tm.const.CROSS_POST_STATE_COMPLETE,
        )
        active_future.set_result(None)

    def test_cross_post_owner_uses_future_registry_for_current_process(self):
        """当前进程无活动 Future 时，同 PID 的新旧 owner 都应视为中断。"""
        stale_owner = f"{tm.socket.gethostname()}:{tm.os.getpid()}:old-instance"

        self.assertFalse(tm._is_cross_post_owner_alive(stale_owner))
        self.assertFalse(tm._is_cross_post_owner_alive(tm._cross_post_process_owner))

    def test_cross_post_owner_detection_handles_process_boundaries(self):
        """所有者探测应覆盖旧记录、其它主机和本机进程异常边界。"""
        hostname = tm.socket.gethostname()

        self.assertFalse(tm._is_cross_post_owner_alive(None))
        self.assertFalse(tm._is_cross_post_owner_alive("invalid-owner"))
        self.assertTrue(tm._is_cross_post_owner_alive("another-host:123:instance"))

        with (
            patch.object(tm.os, "name", "posix"),
            patch.object(tm.os, "kill", side_effect=ProcessLookupError),
        ):
            self.assertFalse(
                tm._is_cross_post_owner_alive(f"{hostname}:987654:dead-instance")
            )
        with (
            patch.object(tm.os, "name", "posix"),
            patch.object(tm.os, "kill", side_effect=PermissionError),
        ):
            self.assertTrue(
                tm._is_cross_post_owner_alive(f"{hostname}:987654:restricted")
            )
        with (
            patch.object(tm.os, "name", "posix"),
            patch.object(tm.os, "kill", side_effect=OSError("inspection failed")),
            patch.object(tm.logger, "warning") as log_warning,
        ):
            self.assertTrue(tm._is_cross_post_owner_alive(f"{hostname}:987654:unknown"))
        self.assertIn("inspection failed", log_warning.call_args.args[0])

        with (
            patch.object(tm.os, "name", "nt"),
            patch.object(tm, "_is_windows_process_alive", return_value=True) as probe,
        ):
            self.assertTrue(tm._is_cross_post_owner_alive(f"{hostname}:987654:windows"))
        probe.assert_called_once_with(987654)

    @unittest.skipUnless(os.name == "nt", "Windows process API test")
    def test_windows_process_probe_is_read_only_and_detects_liveness(self):
        """Windows CI 应真实验证只读进程探测，不允许回退到 os.kill。"""
        self.assertTrue(tm._is_windows_process_alive(os.getpid()))
        self.assertFalse(tm._is_windows_process_alive(2_147_483_647))

    def test_cross_post_terminal_check_converts_active_state_to_failure(self):
        """worker 已结束但状态仍活动时，最终回调必须补写失败终态。"""
        state = MemoryState()
        state.update_task(
            "unfinished-cross-post",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            videos=["final.mp4"],
            cross_post_state=tm.const.CROSS_POST_STATE_PROCESSING,
        )

        with patch.object(tm.sm, "state", state):
            tm._ensure_cross_post_terminal_state("unfinished-cross-post")

        task = state.get_task("unfinished-cross-post")
        self.assertEqual(task["videos"], ["final.mp4"])
        self.assertEqual(task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED)
        self.assertIn("without persisting", task["cross_post_error"])

    def test_cross_post_recovery_reports_state_backend_failure(self):
        """启动恢复读取状态失败时应返回 None，允许 WebUI 后续 rerun 重试。"""
        state = MagicMock()
        state.get_all_tasks.side_effect = RuntimeError("redis unavailable")

        with (
            patch.object(tm.sm, "state", state),
            patch.object(tm.logger, "exception") as log_exception,
        ):
            recovered = tm.recover_interrupted_cross_posts()

        self.assertIsNone(recovered)
        self.assertIn("redis unavailable", log_exception.call_args.args[0])

    def test_cancelled_cross_post_future_releases_slot_and_records_failure(self):
        """排队 Future 被取消时也必须释放容量并写入失败终态。"""
        state = MemoryState()
        state.update_task(
            "cancelled-cross-post",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
        )
        slots = MagicMock()
        future = Future()
        tm._register_cross_post_future("cancelled-cross-post", future)
        self.assertTrue(future.cancel())

        with (
            patch.object(tm.sm, "state", state),
            patch.object(tm, "_cross_post_slots", slots),
        ):
            tm._finalize_cross_post_future("cancelled-cross-post", future)

        slots.release.assert_called_once_with()
        self.assertFalse(tm._is_cross_post_active_in_process("cancelled-cross-post"))
        task = state.get_task("cancelled-cross-post")
        self.assertEqual(task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED)
        self.assertIn("cancelled", task["cross_post_error"])

    @unittest.skipUnless(
        os.getenv("MPT_TEST_REDIS_HOST"),
        "MPT_TEST_REDIS_HOST not set",
    )
    def test_real_redis_recovers_interrupted_cross_post_state(self):
        """真实 Redis 中的遗留发布状态必须在恢复后保留视频并进入失败终态。"""
        state = RedisState(
            host=os.environ["MPT_TEST_REDIS_HOST"],
            port=int(os.getenv("MPT_TEST_REDIS_PORT", "6379")),
            db=int(os.getenv("MPT_TEST_REDIS_DB", "15")),
        )
        task_id = f"ci-cross-post-recovery-{uuid4()}"
        state.update_task(
            task_id,
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            videos=["final.mp4"],
            cross_post_state=tm.const.CROSS_POST_STATE_PROCESSING,
            cross_post_owner="",
        )

        try:
            with patch.object(tm.sm, "state", state):
                recovered = tm.recover_interrupted_cross_posts(page_size=10)

            self.assertGreaterEqual(recovered, 1)
            task = state.get_task(task_id)
            self.assertEqual(task["videos"], ["final.mp4"])
            self.assertEqual(task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED)
            self.assertEqual(task["cross_post_error"], tm._INTERRUPTED_CROSS_POST_ERROR)
        finally:
            state.delete_task(task_id)

    def test_cross_post_future_exception_is_observed(self):
        """线程池自身抛出的异常必须进入日志，不能留在无人读取的 Future 中。"""
        future = Future()
        future.set_exception(RuntimeError("executor worker failed"))

        with patch.object(tm.logger, "error") as log_error:
            tm._finalize_cross_post_future("future-failure", future)

        log_error.assert_called_once()
        self.assertIn("executor worker failed", log_error.call_args.args[0])

    def test_cross_post_queue_full_rejects_only_publishing(self):
        """发布队列满载时必须保留成片，并且不能继续向线程池提交任务。"""
        state = MemoryState()
        state.update_task(
            "cross-post-queue-full",
            state=tm.const.TASK_STATE_COMPLETE,
            progress=100,
            videos=["final.mp4"],
            cross_post_state=tm.const.CROSS_POST_STATE_PENDING,
        )

        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm._cross_post_slots,
                "acquire",
                return_value=False,
            ),
            patch.object(tm._cross_post_executor, "submit") as submit,
        ):
            scheduling_error = tm._schedule_cross_post(
                task_id="cross-post-queue-full",
                video_paths=["final.mp4"],
                params=VideoParams(video_subject="Coffee"),
                video_script="A short coffee story.",
                platforms=["tiktok"],
                youtube_privacy_status="private",
            )

        submit.assert_not_called()
        self.assertIn("queue is full", scheduling_error)
        task = state.get_task("cross-post-queue-full")
        self.assertEqual(task["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(task["videos"], ["final.mp4"])
        self.assertEqual(task["cross_post_state"], tm.const.CROSS_POST_STATE_FAILED)
        self.assertIn("queue is full", task["cross_post_error"])

    @unittest.skipUnless(
        RUN_INTEGRATION_TESTS,
        "MPT_RUN_INTEGRATION_TESTS not set",
    )
    def test_task_local_materials(self):
        task_id = "00000000-0000-0000-0000-000000000000"
        video_materials = []
        for i in range(1, 4):
            video_materials.append(
                MaterialInfo(
                    provider="local",
                    url=os.path.join(resources_dir, f"{i}.png"),
                    duration=0,
                )
            )

        params = VideoParams(
            video_subject="金钱的作用",
            video_script="金钱不仅是交换媒介，更是社会资源的分配工具。它能满足基本生存需求，如食物和住房，也能提供教育、医疗等提升生活品质的机会。拥有足够的金钱意味着更多选择权，比如职业自由或创业可能。但金钱的作用也有边界，它无法直接购买幸福、健康或真诚的人际关系。过度追逐财富可能导致价值观扭曲，忽视精神层面的需求。理想的状态是理性看待金钱，将其作为实现目标的工具而非终极目的。",
            video_terms="money importance, wealth and society, financial freedom, money and happiness, role of money",
            video_aspect="9:16",
            video_concat_mode="random",
            video_transition_mode="None",
            video_clip_duration=3,
            video_count=1,
            video_source="local",
            video_materials=video_materials,
            video_language="",
            voice_name="zh-CN-XiaoxiaoNeural-Female",
            voice_volume=1.0,
            voice_rate=1.0,
            bgm_type="random",
            bgm_file="",
            bgm_volume=0.2,
            subtitle_enabled=True,
            subtitle_position="bottom",
            custom_position=70.0,
            font_name="MicrosoftYaHeiBold.ttc",
            text_fore_color="#FFFFFF",
            text_background_color=True,
            font_size=60,
            stroke_color="#000000",
            stroke_width=1.5,
            n_threads=2,
            paragraph_number=1,
        )
        result = tm.start(task_id=task_id, params=params)
        print(result)


class TestLibraryRehydration(unittest.TestCase):
    """
    默认状态后端只存在于进程内存里，重启即清空。成片仍在磁盘上，
    作品库必须以磁盘为准重建，否则用户每次打开应用都看到空列表。
    """

    def setUp(self):
        self.tasks_root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tasks_root, ignore_errors=True)

    def _make_task(self, task_id, *, final=True, script=None):
        directory = os.path.join(self.tasks_root, task_id)
        os.makedirs(directory)
        if final:
            Path(directory, "final-1.mp4").write_bytes(b"video")
            Path(directory, "combined-1.mp4").write_bytes(b"video")
        Path(directory, "audio.mp3").write_bytes(b"audio")
        if script is not None:
            Path(directory, "script.json").write_text(
                json.dumps({"script": script, "search_terms": ["city"]}),
                encoding="utf-8",
            )
        return directory

    def test_restores_finished_renders_with_their_script(self):
        self._make_task("done-1", script="a narration")
        state = MemoryState()

        with (
            patch.object(tm.utils, "task_dir", return_value=self.tasks_root),
            patch.object(tm.sm, "state", state),
        ):
            restored = tm.rehydrate_finished_tasks_from_disk()

        self.assertEqual(restored, 1)
        task = state.get_task("done-1")
        self.assertEqual(task["state"], tm.const.TASK_STATE_COMPLETE)
        self.assertEqual(task["progress"], 100)
        self.assertEqual(len(task["videos"]), 1)
        self.assertEqual(task["script"], "a narration")

    def test_skips_directories_without_a_finished_video(self):
        """
        没有成片的目录是中断或失败的尝试。把它当作作品库条目，
        用户点开只会得到一个打不开的视频。
        """
        self._make_task("abandoned", final=False, script="never finished")
        state = MemoryState()

        with (
            patch.object(tm.utils, "task_dir", return_value=self.tasks_root),
            patch.object(tm.sm, "state", state),
        ):
            restored = tm.rehydrate_finished_tasks_from_disk()

        self.assertEqual(restored, 0)
        self.assertIsNone(state.get_task("abandoned"))

    def test_never_overwrites_a_task_already_in_the_registry(self):
        """
        正在渲染的任务必须原样保留：用它自己写了一半的输出覆盖状态，
        会让进行中的任务突然显示为已完成。
        """
        self._make_task("running", script="in flight")
        state = MemoryState()
        state.update_task("running", state=tm.const.TASK_STATE_PROCESSING, progress=60)

        with (
            patch.object(tm.utils, "task_dir", return_value=self.tasks_root),
            patch.object(tm.sm, "state", state),
        ):
            restored = tm.rehydrate_finished_tasks_from_disk()

        self.assertEqual(restored, 0)
        self.assertEqual(state.get_task("running")["progress"], 60)

    def test_survives_an_unreadable_script_file(self):
        """script.json 损坏时，视频本身仍然值得出现在作品库里。"""
        directory = self._make_task("broken-script")
        Path(directory, "script.json").write_text("{ not json", encoding="utf-8")
        state = MemoryState()

        with (
            patch.object(tm.utils, "task_dir", return_value=self.tasks_root),
            patch.object(tm.sm, "state", state),
        ):
            restored = tm.rehydrate_finished_tasks_from_disk()

        self.assertEqual(restored, 1)
        self.assertEqual(state.get_task("broken-script")["script"], "")


class TestReviewedRender(unittest.TestCase):
    """
    审阅后的渲染必须复用磁盘上的产物，不能重跑流水线。
    """

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.task_dir = os.path.join(self.temp_root, "task-123")
        os.makedirs(self.task_dir)
        self.audio = os.path.join(self.task_dir, "audio.mp3")
        self.subtitle = os.path.join(self.task_dir, "subtitle.srt")
        self.clip = os.path.join(self.task_dir, "vid-1.mp4")
        for created in (self.audio, self.subtitle, self.clip):
            Path(created).write_bytes(b"data")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_reuses_narration_and_subtitle_without_regenerating(self):
        """
        复用已有 audio.mp3 与 subtitle.srt。走 custom_audio_file 重跑流水线看似
        等价，实则会因为缺少 sub_maker 而静默丢掉字幕。
        """
        params = VideoParams(video_subject="city")
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        with (
            patch.object(tm.utils, "task_dir", return_value=self.task_dir),
            patch.object(tm.sm, "state", state),
            patch.object(tm, "generate_script") as generate_script,
            patch.object(tm, "generate_audio") as generate_audio,
            patch.object(tm, "generate_subtitle") as generate_subtitle,
            patch.object(tm, "_render_and_complete", return_value={"ok": True}) as render,
        ):
            result = tm.render_reviewed_materials("task-123", params, [self.clip])

        self.assertEqual(result, {"ok": True})
        generate_script.assert_not_called()
        generate_audio.assert_not_called()
        generate_subtitle.assert_not_called()

        passed = render.call_args.args
        self.assertEqual(passed[2], [self.clip])
        self.assertEqual(passed[3], self.audio)
        self.assertEqual(passed[4], self.subtitle)
        self.assertEqual(passed[5], 12.0)

    def test_missing_narration_fails_the_task_instead_of_rendering(self):
        """没有配音就渲染只会产出无声成片，必须直接失败并说明原因。"""
        os.remove(self.audio)
        params = VideoParams(video_subject="city")
        state = MemoryState()

        with (
            patch.object(tm.utils, "task_dir", return_value=self.task_dir),
            patch.object(tm.sm, "state", state),
            patch.object(tm, "_render_and_complete") as render,
        ):
            result = tm.render_reviewed_materials("task-123", params, [self.clip])

        render.assert_not_called()
        self.assertEqual(result["failed_stage"], "materials")

    def test_material_deleted_since_review_fails_before_rendering(self):
        """审阅后素材可能被清理；带着缺失文件渲染只会在中途炸。"""
        os.remove(self.clip)
        params = VideoParams(video_subject="city")
        state = MemoryState()

        with (
            patch.object(tm.utils, "task_dir", return_value=self.task_dir),
            patch.object(tm.sm, "state", state),
            patch.object(tm, "_render_and_complete") as render,
        ):
            result = tm.render_reviewed_materials("task-123", params, [self.clip])

        render.assert_not_called()
        self.assertEqual(result["failed_stage"], "materials")

    def test_a_silent_re_render_does_not_need_a_narration_on_disk(self):
        """
        无旁白重渲染时，任务目录里有没有 audio.mp3 都无关紧要。仍然要求它
        存在，会让一个从未录过音的静音项目永远无法再次渲染。
        """
        os.remove(self.audio)
        params = VideoParams(video_subject="city")
        params.voice_enabled = False
        params.video_duration = 20
        state = MemoryState()
        state.update_task("task-123", script="stored script")

        generate_audio, _, render = self._render_with(params, state)

        generate_audio.assert_not_called()
        passed = render.call_args.args
        self.assertEqual(passed[3], "")
        self.assertEqual(passed[4], "")
        self.assertEqual(passed[5], 20)

    def _write_manifest(self, script="stored script", **params):
        """
        The manifest the pipeline leaves behind, naming what the narration on
        disk was recorded from. Without one a re-render cannot tell whether the
        voice changed, and must leave the audio alone.
        """
        recorded = {
            "voice_name": "en-US-AriaNeural-Female",
            "voice_rate": 1.0,
            "subtitle_enabled": True,
        }
        recorded.update(params)
        Path(self.task_dir, "script.json").write_text(
            json.dumps({"script": script, "search_terms": ["city"], "params": recorded}),
            encoding="utf-8",
        )

    def _render_with(self, params, state):
        """Run a reviewed render with every generating stage mocked out."""
        with (
            patch.object(tm.utils, "task_dir", return_value=self.task_dir),
            patch.object(tm.sm, "state", state),
            patch.object(
                tm,
                "generate_audio",
                return_value=(self.audio, 9.0, object()),
            ) as generate_audio,
            patch.object(
                tm, "generate_subtitle", return_value=self.subtitle
            ) as generate_subtitle,
            patch.object(
                tm, "_render_and_complete", return_value={"ok": True}
            ) as render,
        ):
            tm.render_reviewed_materials("task-123", params, [self.clip])
        return generate_audio, generate_subtitle, render

    def test_records_the_narration_again_when_the_voice_changed(self):
        """
        改了配音再渲染，成片却还是旧声音——因为审阅渲染只复用 audio.mp3。
        声音属于录制结果而不是混音参数，参数变了就必须重录。
        """
        self._write_manifest()
        params = VideoParams(video_subject="city")
        params.voice_name = "en-US-GuyNeural-Male"
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        generate_audio, generate_subtitle, render = self._render_with(params, state)

        generate_audio.assert_called_once()
        self.assertEqual(generate_audio.call_args.args[2], "stored script")
        generate_subtitle.assert_called_once()
        # The freshly measured duration, not the one recorded for the old voice:
        # a slower voice makes a longer video, and the clips are cut to fit it.
        self.assertEqual(render.call_args.args[5], 9.0)

    def test_keeps_the_narration_when_only_the_music_changed(self):
        """
        音乐、字幕样式和人声音量都在混音阶段生效，重渲染本来就会带上。
        为它们重跑 TTS 只会白白付一次费用，还会换掉用户满意的声音。
        """
        self._write_manifest()
        params = VideoParams(video_subject="city")
        params.voice_name = "en-US-AriaNeural-Female"
        params.bgm_file = "another-track.mp3"
        params.bgm_volume = 0.6
        params.voice_volume = 1.4
        params.font_size = 72
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        generate_audio, generate_subtitle, render = self._render_with(params, state)

        generate_audio.assert_not_called()
        generate_subtitle.assert_not_called()
        self.assertEqual(render.call_args.args[3], self.audio)
        self.assertEqual(render.call_args.args[5], 12.0)

    def test_records_the_narration_again_when_the_script_changed(self):
        """文案改了却沿用旧配音，成片会念着上一版的稿子。"""
        self._write_manifest(script="stored script")
        params = VideoParams(video_subject="city")
        params.voice_name = "en-US-AriaNeural-Female"
        params.video_script = "a rewritten script"
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        generate_audio, _, render = self._render_with(params, state)

        generate_audio.assert_called_once()
        self.assertEqual(generate_audio.call_args.args[2], "a rewritten script")
        self.assertEqual(render.call_args.args[6], "a rewritten script")

    def test_an_empty_script_field_means_keep_the_generated_one(self):
        """
        LLM 生成的文案只存在任务里，表单一直是空的。把空字段当成“文案被清空”
        会在每次重渲染时重录一遍配音。
        """
        self._write_manifest(script="stored script")
        params = VideoParams(video_subject="city")
        params.voice_name = "en-US-AriaNeural-Female"
        params.video_script = ""
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        generate_audio, _, render = self._render_with(params, state)

        generate_audio.assert_not_called()
        self.assertEqual(render.call_args.args[6], "stored script")

    def test_turning_subtitles_on_records_the_narration_again(self):
        """
        字幕时间轴来自 TTS 的 sub_maker，成品音频事后无法还原（Whisper 除外）。
        因此从关到开必须重录，否则用户打开字幕却什么也看不到。
        """
        self._write_manifest(subtitle_enabled=False)
        params = VideoParams(video_subject="city")
        params.voice_name = "en-US-AriaNeural-Female"
        params.subtitle_enabled = True
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        generate_audio, generate_subtitle, _ = self._render_with(params, state)

        generate_audio.assert_called_once()
        generate_subtitle.assert_called_once()

    def test_turning_subtitles_off_keeps_the_narration(self):
        """关字幕只是少烧录一层，音频本身没变，不值得再付一次 TTS。"""
        self._write_manifest(subtitle_enabled=True)
        params = VideoParams(video_subject="city")
        params.voice_name = "en-US-AriaNeural-Female"
        params.subtitle_enabled = False
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        generate_audio, _, _ = self._render_with(params, state)

        generate_audio.assert_not_called()

    def test_an_older_task_without_a_manifest_keeps_its_narration(self):
        """
        没有清单就无从判断配音是用什么参数录的。此时"猜它变了"会让每次重渲染
        都重跑一次 TTS——对按次计费的供应商就是每次都掏钱。
        """
        params = VideoParams(video_subject="city")
        params.voice_name = "en-US-GuyNeural-Male"
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        generate_audio, _, _ = self._render_with(params, state)

        generate_audio.assert_not_called()

    def test_the_manifest_follows_the_new_voice(self):
        """
        重录后必须改写清单，否则下一次渲染仍然和旧参数比较，每渲染一次就
        重录一次配音。
        """
        self._write_manifest()
        params = VideoParams(video_subject="city")
        params.voice_name = "en-US-GuyNeural-Male"
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        self._render_with(params, state)

        recorded = json.loads(
            Path(self.task_dir, "script.json").read_text(encoding="utf-8")
        )
        self.assertEqual(recorded["params"]["voice_name"], "en-US-GuyNeural-Male")
        # The clip list travels in the task record; repeating it here would
        # bloat the manifest with paths nothing reads back from it.
        self.assertNotIn("materials", recorded["params"])

        generate_audio, _, _ = self._render_with(params, state)
        generate_audio.assert_not_called()

    def test_a_missing_audio_file_is_recorded_again_when_a_script_survives(self):
        """
        缓存清理或手工删除都会带走 audio.mp3。既然文案还在，重录一次就能救回
        这次渲染，比直接失败更有用。
        """
        os.remove(self.audio)
        params = VideoParams(video_subject="city")
        state = MemoryState()
        state.update_task("task-123", script="stored script", audio_duration=12.0)

        generate_audio, generate_subtitle, render = self._render_with(params, state)

        generate_audio.assert_called_once()
        generate_subtitle.assert_called_once()
        self.assertEqual(render.call_args.args[3], self.audio)


class TestMaterialProgressWiring(unittest.TestCase):
    """
    下载进度必须真的从素材层传到任务层，否则进度条依旧停在 40。
    """

    def test_get_video_materials_forwards_progress_callback(self):
        params = VideoParams(video_subject="city")
        params.video_source = "pexels"
        seen = []

        def fake_download_videos(**kwargs):
            kwargs["progress_callback"](0.5, ["/tmp/clip.mp4"])
            return ["/tmp/clip.mp4"]

        with patch.object(
            tm.material, "download_videos", side_effect=fake_download_videos
        ):
            result = tm.get_video_materials(
                "material-progress-wiring",
                params,
                ["city"],
                10,
                progress_callback=lambda fraction, paths: seen.append((fraction, paths)),
            )

        self.assertEqual(result, ["/tmp/clip.mp4"])
        self.assertEqual(seen, [(0.5, ["/tmp/clip.mp4"])])

    def test_downloaded_clips_reach_the_task_record_before_the_batch_ends(self):
        """
        下载要跑几分钟。每落盘一段就要写进任务状态，界面才能立刻显示并播放
        第一段。update_task 是整体替换而非合并，所以进度和片段列表必须在同
        一次调用里写入，否则后一次会把前一次抹掉。
        """
        params = VideoParams(video_subject="city")
        params.video_source = "pexels"
        first = "/tasks/t/vid-1.mp4"
        second = "/tasks/t/vid-2.mp4"

        def fake_get_video_materials(*_args, **kwargs):
            report = kwargs["progress_callback"]
            report(0.5, [first])
            report(1.0, [first, second])
            return [first, second]

        with (
            patch.object(tm, "generate_script", return_value="line one\nline two"),
            patch.object(tm, "generate_terms", return_value=["city"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm, "generate_audio", return_value=("audio.mp3", 5, object())
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(
                tm, "get_video_materials", side_effect=fake_get_video_materials
            ),
            patch.object(tm.sm.state, "update_task") as update_task,
        ):
            tm.start("partial-clips", params, stop_at="materials")

        partial = [
            call.kwargs
            for call in update_task.call_args_list
            if call.kwargs.get("materials") and call.kwargs.get("progress", 100) < 100
        ]

        self.assertTrue(partial, "no clip list was published while downloading")
        self.assertEqual(partial[0]["materials"], [first])
        self.assertTrue(40 <= partial[0]["progress"] <= 50)
        # The review screen pairs each clip with its script paragraph, so the
        # script has to ride along or the partial view has nothing to pair to.
        self.assertEqual(partial[0]["script"], "line one\nline two")

    def test_every_running_write_says_what_the_task_is_doing(self):
        """
        只有百分比时，进度条在一个数字上停几分钟就会被当成卡死。运行中的每
        一次状态写入都必须带上当前动作；``update_task`` 是整体替换而非合并，
        漏掉任何一次都会让文案在下一次写入时消失。
        """
        params = VideoParams(video_subject="city")
        params.video_source = "pexels"

        def fake_get_video_materials(*_args, **kwargs):
            kwargs["progress_callback"](0.5, ["/tasks/t/vid-1.mp4"])
            return ["/tasks/t/vid-1.mp4"]

        def fake_generate_final_videos(*_args, **_kwargs):
            return (["final.mp4"], ["combined.mp4"], [])

        with (
            patch.object(tm, "generate_script", return_value="line"),
            patch.object(tm, "generate_terms", return_value=["city"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm, "generate_audio", return_value=("audio.mp3", 5, object())
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(
                tm, "get_video_materials", side_effect=fake_get_video_materials
            ),
            patch.object(
                tm, "generate_final_videos", side_effect=fake_generate_final_videos
            ),
            patch.object(
                tm.upload_post.upload_post_service, "is_configured", return_value=False
            ),
            patch.object(tm.sm.state, "update_task") as update_task,
        ):
            tm.start("stage-messages", params)

        running = [
            call.kwargs
            for call in update_task.call_args_list
            if call.kwargs.get("state") == tm.const.TASK_STATE_PROCESSING
        ]
        self.assertTrue(running, "the pipeline reported no progress at all")
        for written in running:
            with self.subTest(progress=written.get("progress")):
                self.assertTrue(
                    (written.get("stage_message") or "").strip(),
                    "a running progress write carried no stage message",
                )

    def test_the_download_stage_message_counts_the_clips(self):
        """
        素材下载是最长的阶段之一。带上已下载数量，用户才能看出它在推进，
        而不是停在同一句话上。
        """
        params = VideoParams(video_subject="city")
        params.video_source = "pexels"

        def fake_get_video_materials(*_args, **kwargs):
            kwargs["progress_callback"](0.7, ["/a.mp4", "/b.mp4", "/c.mp4"])
            return ["/a.mp4", "/b.mp4", "/c.mp4"]

        with (
            patch.object(tm, "generate_script", return_value="line"),
            patch.object(tm, "generate_terms", return_value=["city"]),
            patch.object(tm, "save_script_data"),
            patch.object(
                tm, "generate_audio", return_value=("audio.mp3", 5, object())
            ),
            patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
            patch.object(
                tm, "get_video_materials", side_effect=fake_get_video_materials
            ),
            patch.object(tm.sm.state, "update_task") as update_task,
        ):
            tm.start("download-message", params, stop_at="materials")

        messages = [
            call.kwargs.get("stage_message", "")
            for call in update_task.call_args_list
        ]
        self.assertIn("Downloading clips - 3 so far", messages)

    def test_local_materials_need_no_progress_callback(self):
        """
        本地素材已经在磁盘上，没有可上报的进度；这条路径不传回调也不能报错。
        """
        params = VideoParams(video_subject="city")
        params.video_source = "local"
        params.video_materials = [MaterialInfo()]

        with patch.object(
            tm.video, "preprocess_video", return_value=[]
        ), patch.object(tm.sm, "state", MemoryState()):
            result = tm.get_video_materials(
                "local-materials-no-progress", params, ["city"], 10
            )

        self.assertIsNone(result)



class TestFewMaterialsWarning(unittest.TestCase):
    """
    A search that comes back with one usable clip still renders: the cut takes
    every scene out of that one file and the video shows the same few seconds
    over and over. Nothing fails, so until this warning existed nothing said
    anything and the first sign was the finished video.
    """

    def _params(self, **overrides):
        params = VideoParams(video_subject="elephants")
        params.video_source = "pixabay"
        params.video_aspect = VideoAspect.portrait
        params.video_clip_duration = 0
        for key, value in overrides.items():
            setattr(params, key, value)
        return params

    def test_one_clip_for_a_whole_video_is_reported(self):
        warnings = tm._material_shortfall_warnings(self._params(), 1, 34.0)
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["code"], "few_materials")
        self.assertEqual(warning["clips"], 1)
        # The numbers are what the sentence in the UI is built from: how many
        # were found, and how many the video had room for.
        self.assertGreater(warning["wanted"], 1)
        self.assertEqual(warning["source"], "pixabay")
        self.assertEqual(warning["aspect"], "9:16")

    def test_enough_footage_says_nothing(self):
        """The warning has to stay rare enough to still mean something."""
        self.assertEqual(tm._material_shortfall_warnings(self._params(), 4, 34.0), [])

    def test_the_threshold_is_one_clip_per_two_scenes(self):
        # A 34s video plans about four scenes: two clips means each is used
        # twice over, which is the point at which the repetition shows.
        self.assertEqual(len(tm._material_shortfall_warnings(self._params(), 2, 34.0)), 1)
        self.assertEqual(tm._material_shortfall_warnings(self._params(), 3, 34.0), [])

    def test_a_longer_video_needs_more_before_it_is_satisfied(self):
        self.assertEqual(len(tm._material_shortfall_warnings(self._params(), 3, 120.0)), 1)

    def test_the_users_own_files_are_not_second_guessed(self):
        """Nothing narrowed those down; they are the selection."""
        self.assertEqual(
            tm._material_shortfall_warnings(self._params(video_source="local"), 1, 34.0),
            [],
        )

    def test_nothing_downloaded_is_a_failure_elsewhere_not_a_warning_here(self):
        self.assertEqual(tm._material_shortfall_warnings(self._params(), 0, 34.0), [])

    def test_the_finished_task_carries_it(self):
        """
        The task record is what the user reads; a warning that only reaches the
        log is a warning nobody sees. update_task replaces the record rather
        than merging into it, so this is easy to lose by accident.
        """
        state = MemoryState()
        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm,
                "generate_final_videos",
                return_value=(["final-1.mp4"], ["combined-1.mp4"], []),
            ),
        ):
            tm._render_and_complete(
                "few-materials-task",
                self._params(),
                ["only-one.mp4"],
                "audio.mp3",
                "subtitle.srt",
                34.0,
                "script",
                ["elephants"],
            )

        warnings = state.get_task("few-materials-task")["warnings"]
        self.assertEqual([warning["code"] for warning in warnings], ["few_materials"])

    def test_a_render_with_enough_footage_carries_no_warnings(self):
        state = MemoryState()
        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm,
                "generate_final_videos",
                return_value=(["final-1.mp4"], ["combined-1.mp4"], []),
            ),
        ):
            tm._render_and_complete(
                "enough-materials-task",
                self._params(),
                [f"clip-{index}.mp4" for index in range(5)],
                "audio.mp3",
                "subtitle.srt",
                34.0,
                "script",
                ["elephants"],
            )

        self.assertIsNone(state.get_task("enough-materials-task")["warnings"])


if __name__ == "__main__":
    unittest.main()
