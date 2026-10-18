import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from moviepy import (
    ImageClip,
    VideoFileClip,
)

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.models.schema import MaterialInfo
from app.services import video as vd
from app.utils import utils

resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources")


class _FakeMoviePyClip:
    """为最终混音单测提供最小 MoviePy 接口，避免 CI 真实编码大型视频。"""

    def __init__(self, *, duration=5, fps=44100):
        self.duration = duration
        self.fps = fps
        self.close_calls = 0
        self.with_audio_result = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.close_calls += 1

    def with_effects(self, _effects):
        return self

    def with_audio(self, _audio):
        return self.with_audio_result


class TestVideoService(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.test_img_path = os.path.join(resources_dir, "1.png")
        vd._runtime_disabled_video_codecs.clear()
        vd._ffmpeg_encoder_exists.cache_clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        vd._runtime_disabled_video_codecs.clear()
        vd._ffmpeg_encoder_exists.cache_clear()

    def test_delete_files_deduplicates_paths_and_ignores_missing_files(self):
        """
        循环片段会让同一路径在拼接列表中重复出现，清理时每个路径只能删除一次。

        已不存在的文件属于幂等清理的正常状态，不应再产生误导用户的失败日志。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            existing_file = os.path.join(temp_dir, "temp-clip-1.mp4")
            missing_file = os.path.join(temp_dir, "already-removed.mp4")
            Path(existing_file).write_bytes(b"temporary clip")

            original_remove = os.remove
            with (
                patch.object(vd.os, "remove", wraps=original_remove) as remove,
                patch.object(vd.logger, "warning") as warning,
            ):
                vd.delete_files(
                    [
                        existing_file,
                        existing_file,
                        missing_file,
                        missing_file,
                    ]
                )

        self.assertEqual(
            [item.args[0] for item in remove.call_args_list],
            [existing_file, missing_file],
        )
        warning.assert_not_called()

    def test_delete_files_logs_actionable_os_errors(self):
        """权限等真实清理失败必须保留路径和系统错误，方便定位残留文件。"""
        with (
            patch.object(
                vd.os,
                "remove",
                side_effect=PermissionError("permission denied"),
            ),
            patch.object(vd.logger, "warning") as warning,
        ):
            vd.delete_files(["protected-temp-clip.mp4"])

        warning.assert_called_once()
        message = warning.call_args.args[0]
        self.assertIn("protected-temp-clip.mp4", message)
        self.assertIn("permission denied", message)

    def test_generate_video_reports_successful_bgm_mix_and_closes_sources(self):
        """BGM 混合成功后应返回 True，并释放所有原始文件 reader。"""
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=False,
            bgm_type="sonilo",
        )
        source_video = _FakeMoviePyClip()
        voice_source = _FakeMoviePyClip()
        bgm_source = _FakeMoviePyClip()
        mixed_audio = _FakeMoviePyClip(fps=48000)
        final_video = _FakeMoviePyClip()
        source_video.with_audio_result = final_video

        with (
            patch.object(
                vd, "_open_video_clip_quietly", return_value=source_video
            ),
            patch.object(
                vd, "AudioFileClip", side_effect=[voice_source, bgm_source]
            ),
            patch.object(vd, "CompositeAudioClip", return_value=mixed_audio),
            patch.object(vd, "_write_videofile_with_codec_fallback") as writer,
            patch.object(vd, "_get_configured_video_codec", return_value="libx264"),
        ):
            result = vd.generate_video(
                video_path="combined.mp4",
                audio_path="voice.mp3",
                subtitle_path="",
                output_file="final.mp4",
                params=params,
                bgm_file_override="sonilo.m4a",
            )

        self.assertTrue(result)
        writer.assert_called_once()
        self.assertEqual(writer.call_args.kwargs["audio_fps"], 48000)
        self.assertEqual(source_video.close_calls, 1)
        self.assertEqual(voice_source.close_calls, 1)
        self.assertEqual(bgm_source.close_calls, 1)
        self.assertEqual(final_video.close_calls, 1)

    def test_generate_video_keeps_output_and_reports_failed_bgm_mix(self):
        """BGM 打开失败时仍应只写一次无 BGM 视频，并返回 False。"""
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=False,
            bgm_type="sonilo",
        )
        source_video = _FakeMoviePyClip()
        voice_source = _FakeMoviePyClip()
        final_video = _FakeMoviePyClip()
        source_video.with_audio_result = final_video

        with (
            patch.object(
                vd, "_open_video_clip_quietly", return_value=source_video
            ),
            patch.object(
                vd,
                "AudioFileClip",
                side_effect=[voice_source, RuntimeError("invalid BGM")],
            ),
            patch.object(vd, "CompositeAudioClip") as composite_audio,
            patch.object(vd, "_write_videofile_with_codec_fallback") as writer,
            patch.object(vd, "_get_configured_video_codec", return_value="libx264"),
            patch.object(vd.logger, "exception") as log_exception,
        ):
            result = vd.generate_video(
                video_path="combined.mp4",
                audio_path="voice.mp3",
                subtitle_path="",
                output_file="final.mp4",
                params=params,
                bgm_file_override="broken.m4a",
            )

        self.assertFalse(result)
        writer.assert_called_once()
        composite_audio.assert_not_called()
        log_exception.assert_called_once()
        self.assertEqual(source_video.close_calls, 1)
        self.assertEqual(voice_source.close_calls, 1)
        self.assertEqual(final_video.close_calls, 1)

    def test_generate_video_skips_every_bgm_source_when_volume_is_zero(self):
        """0 音量必须在解析文件前统一短路当前来源和未来提供商。"""
        test_cases = [
            ("random", None),
            ("custom", None),
            ("sonilo", "sonilo.m4a"),
            ("future_provider", "future-provider.wav"),
        ]
        for bgm_type, bgm_override in test_cases:
            with self.subTest(bgm_type=bgm_type):
                params = vd.VideoParams(
                    video_subject="test",
                    subtitle_enabled=False,
                    bgm_type=bgm_type,
                    bgm_file="missing-background.mp3",
                    bgm_volume=0.0,
                )
                source_video = _FakeMoviePyClip()
                voice_source = _FakeMoviePyClip()
                final_video = _FakeMoviePyClip()
                source_video.with_audio_result = final_video

                with (
                    patch.object(
                        vd,
                        "_open_video_clip_quietly",
                        return_value=source_video,
                    ),
                    patch.object(
                        vd, "AudioFileClip", return_value=voice_source
                    ) as audio_file_clip,
                    patch.object(vd, "get_bgm_file") as get_bgm_file,
                    patch.object(vd, "CompositeAudioClip") as composite_audio,
                    patch.object(
                        vd, "_write_videofile_with_codec_fallback"
                    ) as writer,
                    patch.object(
                        vd, "_get_configured_video_codec", return_value="libx264"
                    ),
                ):
                    result = vd.generate_video(
                        video_path="combined.mp4",
                        audio_path="voice.mp3",
                        subtitle_path="",
                        output_file="final.mp4",
                        params=params,
                        bgm_file_override=bgm_override,
                    )

                self.assertTrue(result)
                audio_file_clip.assert_called_once_with("voice.mp3")
                get_bgm_file.assert_not_called()
                composite_audio.assert_not_called()
                writer.assert_called_once()
                self.assertEqual(source_video.close_calls, 1)
                self.assertEqual(voice_source.close_calls, 1)
                self.assertEqual(final_video.close_calls, 1)

    def test_generate_video_chooses_looping_by_bgm_file_source(self):
        """默认曲库需要循环，任务层提供的时长适配文件不应依赖提供商名称。"""
        test_cases = [
            ("random", None, True),
            ("custom", None, True),
            ("sonilo", "sonilo.m4a", False),
            ("future_provider", "future-provider.wav", False),
        ]
        for bgm_type, bgm_override, should_loop in test_cases:
            with self.subTest(bgm_type=bgm_type, bgm_override=bgm_override):
                params = vd.VideoParams(
                    video_subject="test",
                    subtitle_enabled=False,
                    bgm_type=bgm_type,
                    bgm_file="library.mp3",
                    bgm_volume=0.2,
                )
                source_video = _FakeMoviePyClip()
                voice_source = _FakeMoviePyClip()
                bgm_source = _FakeMoviePyClip()
                mixed_audio = _FakeMoviePyClip()
                final_video = _FakeMoviePyClip()
                source_video.with_audio_result = final_video

                with (
                    patch.object(
                        vd,
                        "_open_video_clip_quietly",
                        return_value=source_video,
                    ),
                    patch.object(
                        vd,
                        "AudioFileClip",
                        side_effect=[voice_source, bgm_source],
                    ),
                    patch.object(vd, "get_bgm_file", return_value="library.mp3"),
                    patch.object(vd, "CompositeAudioClip", return_value=mixed_audio),
                    patch.object(vd.afx, "AudioLoop") as audio_loop,
                    patch.object(vd, "_write_videofile_with_codec_fallback"),
                    patch.object(
                        vd, "_get_configured_video_codec", return_value="libx264"
                    ),
                ):
                    result = vd.generate_video(
                        video_path="combined.mp4",
                        audio_path="voice.mp3",
                        subtitle_path="",
                        output_file="final.mp4",
                        params=params,
                        bgm_file_override=bgm_override,
                    )

                self.assertTrue(result)
                if should_loop:
                    audio_loop.assert_called_once_with(duration=source_video.duration)
                else:
                    audio_loop.assert_not_called()

    def test_preprocess_video(self):
        if not os.path.exists(self.test_img_path):
            self.fail(f"test image not found: {self.test_img_path}")

        local_videos_dir = utils.storage_dir("local_videos", create=True)
        safe_img_path = os.path.join(local_videos_dir, "test-preprocess-1.png")
        shutil.copy2(self.test_img_path, safe_img_path)

        # test preprocess_video function
        m = MaterialInfo()
        m.url = os.path.basename(safe_img_path)
        m.provider = "local"
        print(m)

        try:
            materials = vd.preprocess_video([m], clip_duration=4)
            print(materials)

            # verify result
            self.assertIsNotNone(materials)
            self.assertEqual(len(materials), 1)
            self.assertTrue(materials[0].url.endswith(".mp4"))

            # moviepy get video info
            clip = VideoFileClip(materials[0].url)
            try:
                print(clip)
            finally:
                clip.close()

            # clean generated test video file
            if os.path.exists(materials[0].url):
                os.remove(materials[0].url)
        finally:
            if os.path.exists(safe_img_path):
                os.remove(safe_img_path)

    def test_generate_video_without_narration_makes_music_the_whole_track(self):
        """
        无旁白视频里，背景音乐就是整条音轨。仍然按"旁白 + BGM"混音会先打开一个
        不存在的音频文件，把一次正常的静音渲染变成崩溃。
        """
        params = vd.VideoParams(
            video_subject="test", subtitle_enabled=False, bgm_type="sonilo"
        )
        source_video = _FakeMoviePyClip()
        bgm_source = _FakeMoviePyClip()
        final_video = _FakeMoviePyClip()
        source_video.with_audio_result = final_video

        with (
            patch.object(vd, "_open_video_clip_quietly", return_value=source_video),
            # One call only: there is no narration file to open.
            patch.object(vd, "AudioFileClip", side_effect=[bgm_source]) as audio_file,
            patch.object(vd, "CompositeAudioClip") as composite,
            patch.object(vd, "_write_videofile_with_codec_fallback") as writer,
            patch.object(vd, "_get_configured_video_codec", return_value="libx264"),
        ):
            result = vd.generate_video(
                video_path="combined.mp4",
                audio_path="",
                subtitle_path="",
                output_file="final.mp4",
                params=params,
                bgm_file_override="sonilo.m4a",
            )

        self.assertTrue(result)
        audio_file.assert_called_once_with("sonilo.m4a")
        # Nothing to mix the music with, so no composite is built at all.
        composite.assert_not_called()
        self.assertIs(writer.call_args.args[0], final_video)

    def test_generate_video_without_narration_or_music_has_no_audio_stream(self):
        """
        既没有旁白也没有音乐时，成片不应该带一条空音轨：MoviePy 会为此单独
        编码一段静音，纯属浪费时间和体积。
        """
        params = vd.VideoParams(
            video_subject="test", subtitle_enabled=False, bgm_type="", bgm_volume=0
        )
        source_video = _FakeMoviePyClip()
        source_video.with_audio_result = _FakeMoviePyClip()

        with (
            patch.object(vd, "_open_video_clip_quietly", return_value=source_video),
            patch.object(vd, "AudioFileClip") as audio_file,
            patch.object(vd, "_write_videofile_with_codec_fallback") as writer,
            patch.object(vd, "_get_configured_video_codec", return_value="libx264"),
        ):
            vd.generate_video(
                video_path="combined.mp4",
                audio_path="",
                subtitle_path="",
                output_file="final.mp4",
                params=params,
            )

        audio_file.assert_not_called()
        # The video clip itself is written, not one wrapped around an audio
        # track that does not exist.
        self.assertIs(writer.call_args.args[0], source_video)

    def test_combine_videos_takes_its_length_from_the_caller_when_silent(self):
        """
        没有旁白就没有可测量的时长。调用方传入的目标时长是唯一的依据，
        否则拼接阶段无从知道该在哪里停。
        """
        with patch.object(vd, "AudioFileClip") as audio_file:
            result = vd.combine_videos(
                combined_video_path="combined.mp4",
                video_paths=[],
                audio_file="",
                target_duration=12,
            )

        # The target is a number, not a file: nothing is opened to find it.
        audio_file.assert_not_called()
        self.assertEqual(result, "combined.mp4")

    def test_combine_videos_refuses_a_silent_cut_with_no_target_length(self):
        """
        静音又没有目标时长时，继续下去只会剪出一段 0 秒的视频。宁可在这里
        明确报错，也不要交付一个空文件。
        """
        with self.assertRaises(ValueError):
            vd.combine_videos(
                combined_video_path="combined.mp4",
                video_paths=[],
                audio_file="",
            )

    def test_preprocess_video_accepts_a_clip_reused_from_the_download_cache(self):
        """
        从库里挑回来的素材，就是上一次渲染下载的那份文件，仍在应用自己的目录内。
        只认 local_videos 会让"重复使用已下载片段"在预处理阶段被静默丢掉：
        用户只看到成片少了几个镜头，日志里也只有一行 unsafe material。
        """
        cache_dir = utils.storage_dir("cache_videos", create=True)
        cached = os.path.join(cache_dir, "vid-preprocess-reuse.mp4")
        Path(cached).write_bytes(b"fake-video")

        class _ReadableClip:
            """Enough of a clip for the resolution check; no decoder involved."""

            size = (1080, 1920)

        try:
            material = MaterialInfo(provider="local", url=os.path.basename(cached))

            with patch.object(
                vd, "_open_video_clip_quietly", return_value=_ReadableClip()
            ):
                materials = vd.preprocess_video([material], clip_duration=4)

            self.assertEqual(len(materials), 1)
            # Resolved where it already is. Copying it into the uploads folder
            # would make the cache that exists to avoid downloading a clip
            # twice cost the disk of a second copy instead.
            self.assertEqual(
                os.path.realpath(materials[0].url), os.path.realpath(cached)
            )
        finally:
            if os.path.exists(cached):
                os.remove(cached)

    def test_preprocess_video_rejects_material_outside_local_videos(self):
        """
        local 素材路径来自 API 参数，不能允许任意绝对路径进入 MoviePy。
        这里验证非 local_videos 白名单目录内的路径会被跳过，避免任意文件读取。
        """
        m = MaterialInfo(provider="local", url=self.test_img_path)

        materials = vd.preprocess_video([m], clip_duration=4)

        self.assertEqual(materials, [])

    def test_get_bgm_file_accepts_song_directory_filename(self):
        """
        BGM 列表接口现在只暴露文件名；生成视频时应能把文件名安全解析回
        resource/songs 白名单目录，保持正常使用路径可用。
        """
        song_dir = utils.song_dir()
        bgm_path = os.path.join(song_dir, "test-safe-bgm.mp3")
        Path(bgm_path).write_bytes(b"fake-mp3")

        try:
            self.assertEqual(vd.get_bgm_file(bgm_file="test-safe-bgm.mp3"), bgm_path)
        finally:
            if os.path.exists(bgm_path):
                os.remove(bgm_path)

    def test_get_bgm_file_accepts_project_relative_song_path(self):
        """
        用户在 WebUI 中可能直接填写 ./resource/songs/xxx.mp3。该路径虽然是
        项目根目录相对路径，但实际文件仍在 resource/songs 白名单目录内，
        应该被接受，避免自定义背景音乐被误判为不存在。
        """
        song_dir = utils.song_dir()
        bgm_path = os.path.join(song_dir, "test-relative-bgm.mp3")
        Path(bgm_path).write_bytes(b"fake-mp3")

        try:
            self.assertEqual(
                vd.get_bgm_file(bgm_file="./resource/songs/test-relative-bgm.mp3"),
                bgm_path,
            )
        finally:
            if os.path.exists(bgm_path):
                os.remove(bgm_path)

    def test_get_bgm_file_rejects_path_outside_song_directory(self):
        """
        用户传入的 bgm_file 不能直接作为本地路径打开，否则可能读取系统文件。
        即使外部文件存在，也必须因为不在 songs 目录内被拒绝。
        """
        with tempfile.NamedTemporaryFile(suffix=".mp3") as temp_bgm:
            self.assertEqual(vd.get_bgm_file(bgm_file=temp_bgm.name), "")

    def test_random_bgm_never_draws_an_uploaded_track(self):
        """
        "random" 只从随应用发布的曲目中抽取。

        上传曲目没有再分发授权，被抽中后会进入用户随后发布的视频；而且
        一次为某个片段上传的文件会永久留在抽签池里，几周后突然成为另一个
        视频的背景音乐。走完整调用链而不是打桩 bundled_bgm_files，因为
        回归恰恰会发生在 get_bgm_file 选错列表的那一行。
        """
        with (
            tempfile.TemporaryDirectory() as builtin_dir,
            tempfile.TemporaryDirectory() as uploaded_dir,
        ):
            Path(builtin_dir, "bundled.mp3").write_bytes(b"builtin")
            Path(uploaded_dir, "my-voice-over.mp3").write_bytes(b"uploaded")

            with (
                patch.object(
                    vd.bgm_service.utils, "song_dir", return_value=builtin_dir
                ),
                patch.object(
                    vd.bgm_service, "uploaded_bgm_dir", return_value=uploaded_dir
                ),
            ):
                # 抽签是随机的，单次通过说明不了问题；池里只有两个文件时，
                # 二十次都命中同一个已经足以排除"偶然没抽到上传曲目"。
                drawn = {vd.get_bgm_file(bgm_type="random") for _ in range(20)}

            self.assertEqual(
                drawn, {os.path.realpath(os.path.join(builtin_dir, "bundled.mp3"))}
            )

    def test_random_bgm_falls_back_to_silence_when_nothing_ships(self):
        """
        内置目录为空时不能改抽上传曲目，也不能让 random.choice([]) 抛异常：
        没有可用曲目就是没有背景音乐。
        """
        with (
            tempfile.TemporaryDirectory() as builtin_dir,
            tempfile.TemporaryDirectory() as uploaded_dir,
        ):
            Path(uploaded_dir, "my-voice-over.mp3").write_bytes(b"uploaded")

            with (
                patch.object(
                    vd.bgm_service.utils, "song_dir", return_value=builtin_dir
                ),
                patch.object(
                    vd.bgm_service, "uploaded_bgm_dir", return_value=uploaded_dir
                ),
            ):
                self.assertEqual(vd.get_bgm_file(bgm_type="random"), "")

    def test_get_ffmpeg_binary_uses_configured_env_path(self):
        """配置中显式指定 ffmpeg 时，应优先使用该路径。"""
        with patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": "/tmp/custom-ffmpeg"}, clear=True):
            self.assertEqual(utils.get_ffmpeg_binary(), "/tmp/custom-ffmpeg")

    def test_get_ffmpeg_binary_falls_back_to_imageio_ffmpeg(self):
        """
        Windows 便携包里系统 PATH 可能没有 ffmpeg，但 moviepy 依赖的
        imageio-ffmpeg 通常会提供可执行文件。这里验证该兜底路径可用。
        """
        fake_imageio_ffmpeg = types.SimpleNamespace(
            get_ffmpeg_exe=lambda: "/tmp/bundled-ffmpeg"
        )

        with patch.dict(os.environ, {}, clear=True), patch.object(
            utils.shutil, "which", return_value=None
        ), patch.dict(sys.modules, {"imageio_ffmpeg": fake_imageio_ffmpeg}):
            self.assertEqual(utils.get_ffmpeg_binary(), "/tmp/bundled-ffmpeg")

    def test_get_effective_video_codec_falls_back_when_encoder_missing(self):
        """
        A requested encoder is probed against FFmpeg's encoder list first, so a
        missing one fails here rather than part-way through writing the file.

        With every probe failing, resolution walks the whole fallback chain and
        lands on its terminal entry. That entry is mpeg4 because FFmpeg always
        builds it in - unlike libx264, which upstream assumed was always there.
        """
        config.app["video_codec"] = "h264_nvenc"

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=False):
            self.assertEqual(vd._get_effective_video_codec(), "mpeg4")

    def test_music_proxy_uses_the_render_encoder_not_libx264(self):
        """
        The AI music services upload a proxy of the cut. Upstream encoded it
        with libx264, which this LGPL FFmpeg does not have, so the proxy - and
        with it every Sonilo and ElevenLabs Music request - failed.
        """
        config.app["video_codec"] = "h264_mf"

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
            args = vd.music_proxy_codec_args()

        self.assertEqual(args[:2], ["-c:v", "h264_mf"])
        self.assertNotIn("libx264", args)

    def test_get_effective_video_codec_prefers_first_available_in_chain(self):
        """
        When the requested encoder is unavailable but others are present, the
        first working entry in the chain wins rather than a fixed codec.
        """
        config.app["video_codec"] = "h264_nvenc"
        available = {"libopenh264", "libx264"}

        with patch.object(
            vd,
            "_ffmpeg_encoder_exists",
            side_effect=lambda _binary, codec: codec in available,
        ):
            self.assertEqual(vd._get_effective_video_codec(), "libopenh264")

    def test_get_configured_video_codec_uses_stable_default_when_unset(self):
        """
        With video_codec unset the backend must still name an encoder rather
        than leaving the choice to MoviePy or FFmpeg.

        The default is h264_mf, not libx264: the application ships an LGPL
        FFmpeg build, which excludes libx264 entirely.
        """
        config.app.pop("video_codec", None)

        self.assertEqual(vd._get_configured_video_codec(), "h264_mf")

    def test_get_configured_video_codec_preserves_explicit_libx264(self):
        """
        用户明确选择 libx264 时需要保持固定选择。它与“跟随项目默认策略”当前
        结果相同，但配置语义不同，未来调整默认值时不能影响显式选择。
        """
        config.app["video_codec"] = "libx264"

        self.assertEqual(vd._get_configured_video_codec(), "libx264")

    def test_ffmpeg_encoder_exists_falls_back_when_probe_fails(self):
        """
        Windows 上用户配置的 ffmpeg 可能因为路径损坏、权限或杀软拦截而无法
        正常执行。encoder 探测失败时必须返回 False，让上层稳定回退 libx264。
        """
        with patch.object(
            vd.subprocess,
            "run",
            side_effect=OSError("permission denied"),
        ):
            self.assertFalse(vd._ffmpeg_encoder_exists("C:/ffmpeg/bin/ffmpeg.exe", "h264_nvenc"))

    def test_write_videofile_falls_back_after_runtime_encoder_failure(self):
        """
        FFmpeg 声明支持某个硬件编码器，不代表当前显卡或驱动一定可用。
        首次实际编码失败后，应立即用 libx264 重试，并在本进程禁用该编码器。
        """

        class _FakeClip:
            def __init__(self):
                self.codecs = []

            def write_videofile(self, output_file, codec, **kwargs):
                self.codecs.append(codec)
                if codec == "h264_nvenc":
                    raise RuntimeError("nvenc device not available")

        fake_clip = _FakeClip()

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
            used_codec = vd._write_videofile_with_codec_fallback(
                fake_clip,
                "/tmp/fake.mp4",
                codec="h264_nvenc",
                logger=None,
                fps=30,
            )

        # The retry uses the resolved head of the fallback chain; with every
        # encoder reported present that is h264_mf.
        self.assertEqual(used_codec, "h264_mf")
        self.assertEqual(fake_clip.codecs, ["h264_nvenc", "h264_mf"])
        self.assertIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_write_videofile_does_not_disable_codec_when_fallback_also_fails(self):
        """
        如果 libx264 兜底也失败，失败原因更可能是输出路径、权限、文件占用等
        通用问题，不能误判为硬件编码器不可用。
        """

        class _FakeClip:
            def write_videofile(self, output_file, codec, **kwargs):
                raise RuntimeError(f"{codec} cannot write output")

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
            with self.assertRaises(RuntimeError):
                vd._write_videofile_with_codec_fallback(
                    _FakeClip(),
                    "/tmp/fake.mp4",
                    codec="h264_nvenc",
                    logger=None,
                    fps=30,
                )

        self.assertNotIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_bundled_fonts_script_coverage(self):
        """
        Pin what each bundled subtitle font can actually render.

        Upstream shipped Microsoft YaHei, STHeiti and UTM Kabel, which between
        them covered CJK, Cyrillic and Greek. All three are proprietary and
        cannot be redistributed, so they were removed. Noto Sans was added on
        2026-08-30 for the Romanian letters Be Vietnam Pro lacks, and brought
        Cyrillic and Greek back with it - which is why the expectation is now
        per font rather than one rule for all of them.

        This is a user-visible limitation, not an implementation detail: a
        Chinese subtitle silently renders as blank glyphs. The test exists so
        that adding or removing a face is a deliberate act that updates this
        table and LICENSES-third-party.md with it.
        """
        latin = "Sample subtitle line"
        samples = {
            "romanian": "Știți că țara are munți",
            "vietnamese": "Tôi vẫn luôn tin vào tương lai",
            "cyrillic": "Это текст для проверки",
            "greek": "Αυτό είναι κείμενο",
            "thai": "นี่คือข้อความ",
            "chinese": "这是一个测试句子",
        }
        # What each bundled face covers, measured rather than assumed. Every
        # font on disk has to appear here, and nothing may cover Chinese: that
        # is the gap the licensing left behind.
        coverage = {
            "NotoSans-Bold.ttf": {"romanian", "vietnamese", "cyrillic", "greek"},
            "NotoSans-Medium.ttf": {"romanian", "vietnamese", "cyrillic", "greek"},
            "BeVietnamPro-Bold.ttf": {"vietnamese"},
            "BeVietnamPro-Medium.ttf": {"vietnamese"},
            "Charm-Regular.ttf": {"romanian", "vietnamese", "thai"},
            "Charm-Bold.ttf": {"romanian", "vietnamese", "thai"},
        }

        font_names = [
            name
            for name in os.listdir(utils.font_dir())
            if name.lower().endswith((".ttf", ".ttc", ".otf"))
        ]
        self.assertTrue(font_names, "no subtitle fonts are bundled")
        self.assertEqual(
            sorted(font_names),
            sorted(coverage),
            "the bundled fonts changed; update this table, "
            "LICENSES-third-party.md and licenses/THIRD-PARTY-NOTICES.md",
        )

        for font_name in font_names:
            font_path = os.path.join(utils.font_dir(), font_name)
            with self.subTest(font=font_name):
                self.assertTrue(
                    vd.subtitle_font_supports_text(font_path, latin),
                    f"{font_name} cannot render basic Latin text",
                )
                for script, text in samples.items():
                    covered = script in coverage[font_name]
                    self.assertEqual(
                        vd.subtitle_font_supports_text(font_path, text),
                        covered,
                        f"{font_name} no longer {'covers' if covered else 'lacks'} "
                        f"{script}; update this table and the licence notes if the "
                        "bundle changed deliberately",
                    )

    def test_the_default_subtitle_font_draws_romanian_as_it_is_spelled(self):
        """
        The default face has to have the letters itself. Falling back to
        `subtitle_text_for_font` is a repair for a font the user chose, not a
        state the app should ship in - it costs the reader the difference
        between s-comma and s-cedilla on every line.
        """
        default_font = vd.VideoParams(video_subject="anything").font_name
        font_path = os.path.join(utils.font_dir(), default_font)
        line = "Știți că țara are munți și șosele"

        self.assertTrue(os.path.exists(font_path), f"{default_font} is not bundled")
        self.assertTrue(vd.subtitle_font_supports_text(font_path, line))
        self.assertEqual(vd.subtitle_text_for_font(line, font_path), line)

    def test_a_letter_the_font_lacks_is_swapped_for_the_one_it_has(self):
        """
        Romanian s-comma and t-comma are missing from Be Vietnam Pro, which
        draws only the cedilla forms that encoded the same two letters before
        Unicode 3.0. A missing glyph is not drawn at all, so without the swap a
        correctly spelled line arrives on screen with holes where two of its
        letters should be. Noto Sans is the default now and has the letters, but
        Be Vietnam Pro still ships and can still be chosen.
        """
        font_path = os.path.join(utils.font_dir(), "BeVietnamPro-Bold.ttf")
        line = "Știți că țara are munți."

        drawn = vd.subtitle_text_for_font(line, font_path)

        self.assertEqual(drawn, "Ştiţi că ţara are munţi.")
        self.assertTrue(vd.subtitle_font_supports_text(font_path, drawn))

    def test_a_font_that_has_the_letter_keeps_the_spelling(self):
        """The swap is a repair for one font's gap, not a rewrite of Romanian."""
        font_path = os.path.join(utils.font_dir(), "Charm-Bold.ttf")
        line = "Știți că țara are munți."

        self.assertEqual(vd.subtitle_text_for_font(line, font_path), line)

    def test_a_letter_with_no_equivalent_falls_back_to_its_base(self):
        """
        Misspelling a word is a small loss next to a gap where a letter should
        be, and a reader puts the accent back.
        """
        font_path = os.path.join(utils.font_dir(), "BeVietnamPro-Bold.ttf")

        self.assertEqual(vd.subtitle_text_for_font("ģimene", font_path), "gimene")

    def test_an_uncovered_script_is_not_quietly_rewritten(self):
        """
        Nothing a Chinese character decomposes to is a Latin letter, so the
        substitution has nothing to offer and must not invent one: an uncovered
        script still fails visibly instead of saying something it never said.
        """
        font_path = os.path.join(utils.font_dir(), "BeVietnamPro-Bold.ttf")

        self.assertEqual(vd.subtitle_text_for_font("这是一个测试", font_path), "这是一个测试")

    def test_format_ffmpeg_concat_path_normalizes_windows_path(self):
        """
        concat demuxer 的文件列表对 Windows 反斜杠较敏感，写入 list 前统一
        转成正斜杠，并继续保留单引号转义。
        """
        with patch.object(
            vd.os.path,
            "abspath",
            return_value=r"C:\Users\Test User's Videos\clip.mp4",
        ):
            self.assertEqual(
                vd._format_ffmpeg_concat_path(
                    r"C:\Users\Test User's Videos\clip.mp4"
                ),
                "C:/Users/Test User'\\''s Videos/clip.mp4",
            )

    def test_concat_video_clips_falls_back_after_runtime_encoder_failure(self):
        """
        最终 ffmpeg concat 阶段也要具备同样的回退能力。这里用 mock 模拟
        h264_nvenc 编码失败，确认会自动再用 libx264 执行一次。
        """
        config.app["video_codec"] = "h264_nvenc"

        def fake_run(command, capture_output, text, check, **kwargs):
            codec_index = command.index("-c:v") + 1
            codec = command[codec_index]
            if codec == "h264_nvenc":
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="nvenc device not available",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
                with patch.object(vd.subprocess, "run", side_effect=fake_run) as run:
                    vd.concat_video_clips_with_ffmpeg(
                        clip_files=[clip_file],
                        output_file=output_file,
                        threads=1,
                        output_dir=temp_dir,
                    )

        used_codecs = [
            call.args[0][call.args[0].index("-c:v") + 1]
            for call in run.call_args_list
        ]
        self.assertEqual(used_codecs, ["h264_nvenc", "h264_mf"])
        self.assertIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_concat_video_clips_does_not_disable_codec_when_fallback_also_fails(self):
        """
        concat 阶段如果 libx264 也失败，说明可能是输入 list、路径或输出权限
        问题，不能把硬件编码器加入运行时禁用列表。
        """
        config.app["video_codec"] = "h264_nvenc"

        def fake_run(command, capture_output, text, check, **kwargs):
            codec_index = command.index("-c:v") + 1
            codec = command[codec_index]
            return types.SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"{codec} cannot write output",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
                with patch.object(vd.subprocess, "run", side_effect=fake_run):
                    with self.assertRaises(RuntimeError):
                        vd.concat_video_clips_with_ffmpeg(
                            clip_files=[clip_file],
                            output_file=output_file,
                            threads=1,
                            output_dir=temp_dir,
                        )

        self.assertNotIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_open_video_clip_quietly_suppresses_moviepy_stdout(self):
        """
        MoviePy 2.1.x 的 FFMPEG_VideoReader 会直接向 stdout 打印 metadata
        和 ffmpeg 命令。项目服务层应屏蔽这类依赖库噪声，避免用户把
        `audio_found: False` 误判为最终视频没有音频。
        """
        # 测试只关心服务层是否屏蔽 MoviePy 的读取噪声，不应长期保存一份由 PNG
        # 编码而来的二进制 MP4 fixture。运行时生成短视频既能保持测试独立，也能
        # 避免 fixture 因不同编码参数产生帧间闪烁后被误用于视觉效果验证。
        image_path = os.path.join(resources_dir, "1.png")
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = os.path.join(temp_dir, "image-fixture.mp4")
            source_clip = ImageClip(image_path).with_duration(0.2)
            try:
                source_clip.write_videofile(
                    video_path,
                    codec="libx264",
                    fps=5,
                    audio=False,
                    logger=None,
                )
            finally:
                source_clip.close()

            stdout = StringIO()
            with redirect_stdout(stdout):
                clip = vd._open_video_clip_quietly(video_path)

            try:
                self.assertEqual(stdout.getvalue(), "")
                self.assertIsNone(clip.audio)
                self.assertGreater(clip.duration, 0)
            finally:
                vd.close_clip(clip)

    def test_combine_videos_closes_audio_clip_when_duration_read_fails(self):
        """
        `combine_videos()` 只需要读取旁白音频时长。即使读取 duration
        时发生异常，也必须关闭 AudioFileClip，避免文件句柄泄漏。
        """

        class _FakeAudioReader:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _BrokenAudioClip:
            def __init__(self):
                self.reader = _FakeAudioReader()

            @property
            def duration(self):
                raise RuntimeError("failed to read duration")

        fake_audio_clip = _BrokenAudioClip()

        with patch.object(vd, "AudioFileClip", return_value=fake_audio_clip):
            with self.assertRaises(RuntimeError):
                vd.combine_videos(
                    combined_video_path="/tmp/unused-combined.mp4",
                    video_paths=[],
                    audio_file="/tmp/unused-audio.mp3",
                )

        self.assertTrue(fake_audio_clip.reader.closed)

    def test_combine_videos_handles_none_transition_mode(self):
        """
        Ensure `combine_videos` safely handles
        `video_transition_mode=None`.
        """
        class _FakeAudioClip:
            @property
            def duration(self):
                return 10.0

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")
            audio_file = os.path.join(temp_dir, "audio.mp3")

            with patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()):
                # Use empty video_paths to avoid heavy video processing while
                # still exercising transition mode normalization logic.
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=[],
                    audio_file=audio_file,
                    video_transition_mode=None,
                )
                self.assertEqual(result, combined_video_path)

    def _capture_source_ranges_for_clip_speed(
        self,
        *,
        source_duration,
        audio_duration,
        clip_speed,
        max_clip_duration=3,
    ):
        """使用轻量假视频记录 combine_videos 实际读取的源时间范围。"""

        source_ranges = []
        written_durations = []

        class _FakeAudioClip:
            duration = audio_duration

            def close(self):
                pass

        class _FakeVideoClip:
            def __init__(self, duration, records_source_range=False):
                self.duration = duration
                self.size = (1080, 1920)
                self.w = 1080
                self.h = 1920
                self.records_source_range = records_source_range

            def subclipped(self, start_time, end_time):
                # 只记录直接从源文件读取的范围。变速后的安全裁剪也会调用
                # subclipped，但它不代表新的源时间段，不能混入断层判断。
                if self.records_source_range:
                    source_ranges.append((start_time, end_time))
                return _FakeVideoClip(end_time - start_time)

            def with_speed_scaled(self, factor):
                return _FakeVideoClip(self.duration / factor)

            def close(self):
                pass

        def _open_fake_video_clip(_video_path):
            return _FakeVideoClip(source_duration, records_source_range=True)

        def _capture_written_clip(clip, *_args, **_kwargs):
            written_durations.append(clip.duration)

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")
            with (
                patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
                patch.object(
                    vd,
                    "_open_video_clip_quietly",
                    side_effect=_open_fake_video_clip,
                ),
                patch.object(
                    vd,
                    "_write_videofile_with_codec_fallback",
                    side_effect=_capture_written_clip,
                ),
                # random 模式默认会打乱同一源视频的切片。这里保持生成顺序，
                # 才能精确验证相邻源时间段是否连续。
                patch.object(
                    vd,
                    "_prioritize_unique_source_clips",
                    side_effect=lambda subclipped_items, concat_mode: subclipped_items,
                ),
                patch.object(vd, "concat_video_clips_with_ffmpeg"),
                patch.object(vd, "delete_files"),
            ):
                vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["clip.mp4"],
                    audio_file="audio.mp3",
                    video_concat_mode=vd.VideoConcatMode.random,
                    max_clip_duration=max_clip_duration,
                    clip_speed=clip_speed,
                )

        return source_ranges, written_durations

    def test_combine_videos_slow_speed_keeps_source_timeline_continuous(self):
        """0.5 倍慢放应连续读取 1.5 秒源片段，不能跳过中间画面。"""

        source_ranges, written_durations = self._capture_source_ranges_for_clip_speed(
            source_duration=4.0,
            audio_duration=5.9,
            clip_speed=0.5,
        )

        self.assertEqual(source_ranges, [(0, 1.5), (1.5, 3.0)])
        self.assertEqual(written_durations, [3.0, 3.0])

    def test_combine_videos_fast_speed_reads_enough_source_content(self):
        """2 倍快放应读取 6 秒源画面，使最终片段仍保持 3 秒。"""

        source_ranges, written_durations = self._capture_source_ranges_for_clip_speed(
            source_duration=8.0,
            audio_duration=2.9,
            clip_speed=2.0,
        )

        self.assertEqual(source_ranges, [(0, 6.0)])
        self.assertEqual(written_durations, [3.0])

    def test_combine_videos_keeps_small_duration_safety_margin(self):
        """
        音频和素材累计时长刚好相等时，仍应继续追加一个短片段作为安全余量。

        FFmpeg 按帧率拼接后可能让最终视频比理论时长短几十毫秒。如果这里
        在 10.0s == 10.0s 时立即停止，成片末尾就可能出现音频还在播放但
        视频素材已经结束的边界问题。
        """

        class _FakeAudioClip:
            duration = 10.0

            def close(self):
                pass

        class _FakeVideoClip:
            def __init__(self, duration):
                self.duration = duration
                self.size = (1080, 1920)
                self.w = 1080
                self.h = 1920

            def subclipped(self, start_time, end_time):
                return _FakeVideoClip(end_time - start_time)

        video_durations = {
            "clip-1.mp4": 3.0,
            "clip-2.mp4": 4.0,
            "clip-3.mp4": 3.0,
            "clip-4.mp4": 2.0,
        }

        def _open_fake_video_clip(video_path):
            return _FakeVideoClip(video_durations[video_path])

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")

            with patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()):
                with patch.object(
                    vd, "_open_video_clip_quietly", side_effect=_open_fake_video_clip
                ):
                    with patch.object(
                        vd, "_write_videofile_with_codec_fallback"
                    ) as write_mock:
                        with patch.object(vd, "concat_video_clips_with_ffmpeg") as concat_mock:
                            with patch.object(vd, "delete_files"):
                                result = vd.combine_videos(
                                    combined_video_path=combined_video_path,
                                    video_paths=list(video_durations.keys()),
                                    audio_file=os.path.join(temp_dir, "audio.mp3"),
                                    video_aspect=vd.VideoAspect.portrait,
                                    video_concat_mode=vd.VideoConcatMode.sequential,
                                    video_transition_mode=None,
                                    max_clip_duration=10,
                                )

        self.assertEqual(result, combined_video_path)
        self.assertEqual(write_mock.call_count, 4)
        self.assertEqual(concat_mock.call_args.kwargs["max_duration"], 10.0)

    def test_concat_video_clips_limits_output_to_audio_duration(self):
        """最终拼接时应裁到音频时长，避免安全余量带来明显静音尾巴。"""

        def fake_run(command, capture_output, text, check, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            with patch.object(vd.subprocess, "run", side_effect=fake_run) as run:
                vd.concat_video_clips_with_ffmpeg(
                    clip_files=[clip_file],
                    output_file=output_file,
                    threads=1,
                    output_dir=temp_dir,
                    max_duration=10.0,
                )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "10.000")
        self.assertLess(command.index("-t"), command.index(output_file))

    def test_prioritize_unique_source_clips_uses_each_source_before_reuse(self):
        """
        随机模式下，一个长素材会被拆成多个片段。调度层应先让每个源素材
        至少出现一次，再使用同一源素材的其他切片，降低用户感知到的重复。
        """
        clips = [
            vd.SubClippedVideoClip("a.mp4", 0, 4, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("a.mp4", 4, 8, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("b.mp4", 0, 4, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("b.mp4", 4, 8, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("c.mp4", 0, 4, source_file_path="c.mp4"),
        ]

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=clips,
            concat_mode=vd.VideoConcatMode.random,
        )

        self.assertCountEqual(ordered_clips, clips)
        first_round_sources = [clip.source_file_path for clip in ordered_clips[:3]]
        self.assertCountEqual(first_round_sources, ["a.mp4", "b.mp4", "c.mp4"])

    def test_prioritize_unique_source_clips_keeps_sequential_order(self):
        """
        顺序模式本身只取每个素材的首段，不应被随机调度逻辑改变顺序。
        """
        clips = [
            vd.SubClippedVideoClip("a.mp4", 0, 4, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("b.mp4", 0, 4, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("c.mp4", 0, 4, source_file_path="c.mp4"),
        ]

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=clips,
            concat_mode=vd.VideoConcatMode.sequential,
        )

        self.assertEqual(ordered_clips, clips)

    def test_prioritize_unique_source_clips_prefers_long_primary_clip(self):
        """
        同一个源素材的最后一个切片可能短于目标片段时长。首轮去重时应优先
        选择较长片段，否则会因为累计时长不足而提前复用素材。
        """
        short_tail = vd.SubClippedVideoClip(
            "a.mp4", 6, 6.5, source_file_path="a.mp4"
        )
        full_clip = vd.SubClippedVideoClip(
            "a.mp4", 0, 3, source_file_path="a.mp4"
        )
        other_source = vd.SubClippedVideoClip(
            "b.mp4", 0, 3, source_file_path="b.mp4"
        )

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=[short_tail, full_clip, other_source],
            concat_mode=vd.VideoConcatMode.random,
        )

        first_a_clip = next(
            clip for clip in ordered_clips if clip.source_file_path == "a.mp4"
        )
        self.assertEqual(first_a_clip, full_clip)

    def test_prioritize_unique_source_clips_advances_past_each_opening(self):
        """
        A second visit to a source must land on the stretch after the one
        already used. Ordering every opening first and shuffling the rest
        behind it meant that a render with enough footage to cover the
        narration stopped before the remainder and never showed anything past
        the first few seconds of any clip.
        """
        clips = [
            vd.SubClippedVideoClip("a.mp4", 0, 5, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("a.mp4", 5, 10, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("a.mp4", 10, 15, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("b.mp4", 0, 5, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("b.mp4", 5, 10, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("b.mp4", 10, 15, source_file_path="b.mp4"),
        ]

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=clips,
            concat_mode=vd.VideoConcatMode.random,
        )

        self.assertCountEqual(ordered_clips, clips)
        # One round per segment index: both openings, then both second
        # stretches, then both third.
        rounds = [ordered_clips[0:2], ordered_clips[2:4], ordered_clips[4:6]]
        for round_index, round_clips in enumerate(rounds):
            expected_start = round_index * 5
            self.assertCountEqual(
                [clip.source_file_path for clip in round_clips],
                ["a.mp4", "b.mp4"],
            )
            for clip in round_clips:
                self.assertEqual(clip.start_time, expected_start)

    def test_prioritize_unique_source_clips_sequential_rounds_in_file_order(self):
        """
        "Keep my order" asks for the sources in the order they were given.
        It does not ask for only their openings: the segments behind them are
        reached in later rounds, in that same order.
        """
        clips = [
            vd.SubClippedVideoClip("a.mp4", 0, 5, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("a.mp4", 5, 10, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("b.mp4", 0, 5, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("b.mp4", 5, 10, source_file_path="b.mp4"),
        ]

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=clips,
            concat_mode=vd.VideoConcatMode.sequential,
        )

        self.assertEqual(
            [(clip.source_file_path, clip.start_time) for clip in ordered_clips],
            [("a.mp4", 0), ("b.mp4", 0), ("a.mp4", 5), ("b.mp4", 5)],
        )

    def test_prioritize_unique_source_clips_leaves_short_tail_for_last(self):
        """
        The leftover at the end of a file is shorter than a scene. It belongs
        in the last round rather than winning a round it only entered because
        the caller happened to list it first.
        """
        tail = vd.SubClippedVideoClip("a.mp4", 5, 6, source_file_path="a.mp4")
        opening = vd.SubClippedVideoClip("a.mp4", 0, 5, source_file_path="a.mp4")

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=[tail, opening],
            concat_mode=vd.VideoConcatMode.sequential,
        )

        self.assertEqual(ordered_clips, [opening, tail])

    def test_combine_videos_sequential_never_replays_an_unused_stretch(self):
        """
        Sequential used to stop slicing after the first segment of each file,
        so a narration longer than one scene per clip was covered by replaying
        the openings already used. Five clips became the same five openings on
        a loop, and nothing behind them was ever seen.
        """
        used_ranges = []

        class _FakeAudioClip:
            duration = 45.0

            def close(self):
                pass

        class _FakeVideoClip:
            def __init__(self, duration, source_path=None):
                self.duration = duration
                self.size = (1080, 1920)
                self.w = 1080
                self.h = 1920
                self.source_path = source_path

            def subclipped(self, start_time, end_time):
                if self.source_path is not None:
                    used_ranges.append((self.source_path, start_time, end_time))
                return _FakeVideoClip(end_time - start_time)

            def close(self):
                pass

        video_paths = ["clip1.mp4", "clip2.mp4", "clip3.mp4"]

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
                patch.object(
                    vd,
                    "_open_video_clip_quietly",
                    side_effect=lambda path: _FakeVideoClip(20.0, source_path=path),
                ),
                # Force the MoviePy branch: the fast path shells out to FFmpeg,
                # which has no real file to read here.
                patch.object(vd, "_render_clip_with_ffmpeg", return_value=False),
                patch.object(vd, "_write_videofile_with_codec_fallback"),
                patch.object(vd, "concat_video_clips_with_ffmpeg"),
                patch.object(vd, "delete_files"),
            ):
                vd.combine_videos(
                    combined_video_path=os.path.join(temp_dir, "combined.mp4"),
                    video_paths=video_paths,
                    audio_file="audio.mp3",
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    max_clip_duration=5,
                )

        self.assertEqual(
            len(used_ranges),
            len(set(used_ranges)),
            f"a stretch was used twice while others went unused: {used_ranges}",
        )
        # The order asked for, rounded through: every opening, then every
        # second stretch, and so on.
        self.assertEqual(
            used_ranges[:6],
            [
                ("clip1.mp4", 0, 5),
                ("clip2.mp4", 0, 5),
                ("clip3.mp4", 0, 5),
                ("clip1.mp4", 5, 10),
                ("clip2.mp4", 5, 10),
                ("clip3.mp4", 5, 10),
            ],
        )
    
    def test_wrap_text(self):
        """test text wrapping function"""
        try:
            font_path = os.path.join(utils.font_dir(), "BeVietnamPro-Bold.ttf")
            if not os.path.exists(font_path):
                self.fail(f"font file not found: {font_path}")
                
            # test english text wrapping
            test_text_en = "This is a test text for wrapping long sentences in english language"
            
            wrapped_text_en, text_height_en = vd.wrap_text(
                text=test_text_en,
                max_width=300,
                font=font_path,
                fontsize=30
            )
            print(wrapped_text_en, text_height_en)
            # verify text is wrapped
            self.assertIn("\n", wrapped_text_en)
            
            # test chinese text wrapping
            test_text_zh = "这是一段用来测试中文长句换行的文本内容，应该会根据宽度限制进行换行处理"
            wrapped_text_zh, text_height_zh = vd.wrap_text(
                text=test_text_zh,
                max_width=300,
                font=font_path,
                fontsize=30
            )   
            print(wrapped_text_zh, text_height_zh)
            # verify chinese text is wrapped
            self.assertIn("\n", wrapped_text_zh)
        except Exception as e:
            self.fail(f"test wrap_text failed: {str(e)}")

    def test_wrap_text_uses_stable_line_metrics_for_all_bundled_fonts(self):
        """
        字幕高度必须来自字体自身的 ascent/descent，而不能取决于当前文字。

        不含 g/j/p/q/y 的拉丁文本只有大写字母和 x-height，Pillow 的字形
        bbox 会比字体真实行高短很多；多行时误差累积，最终会裁掉最后一行。
        这里遍历全部内置字体，并同时覆盖含下伸部与不含下伸部的英文文本，
        防止以后重新引入“按当前字形墨迹计算行高”的实现。
        """
        font_size = 60
        max_width = 360
        text_cases = {
            "without_descenders": "A man survived the Hiroshima atomic bomb blast",
            "with_descenders": "Typing quickly brings joyful progress",
        }
        font_paths = sorted(
            path
            for path in Path(utils.font_dir()).iterdir()
            if path.suffix.lower() in {".ttf", ".ttc"}
        )

        self.assertTrue(font_paths, "expected bundled subtitle fonts")
        for font_path in font_paths:
            font = vd.ImageFont.truetype(str(font_path), font_size)
            expected_line_height = sum(font.getmetrics())
            for case_name, text in text_cases.items():
                with self.subTest(font=font_path.name, case=case_name):
                    wrapped_text, text_height = vd.wrap_text(
                        text=text,
                        max_width=max_width,
                        font=str(font_path),
                        fontsize=font_size,
                    )
                    line_count = wrapped_text.count("\n") + 1

                    self.assertGreater(line_count, 1)
                    self.assertEqual(
                        text_height,
                        line_count * expected_line_height,
                    )

    def test_wrap_text_counts_existing_subtitle_line_breaks(self):
        """
        SRT 文本可能已经包含人工换行；即使每行都不需要再次折行，高度也必须
        按最终两行计算。否则宽画面上的短句会绕过自动换行分支并再次裁掉末行。
        """
        font_size = 60
        font_path = os.path.join(utils.font_dir(), "BeVietnamPro-Bold.ttf")
        text = "SAFE TEXT\nMORE SAFE"
        font = vd.ImageFont.truetype(font_path, font_size)

        wrapped_text, text_height = vd.wrap_text(
            text=text,
            max_width=972,
            font=font_path,
            fontsize=font_size,
        )

        self.assertEqual(wrapped_text, text)
        self.assertEqual(text_height, 2 * sum(font.getmetrics()))

    def test_small_subtitle_with_thick_stroke_keeps_a_bottom_margin(self):
        """
        小字号配粗描边是最容易重新触底的比例边界。遍历全部内置字体并读取
        MoviePy 的真实 mask，确保额外高度至少容纳向上下扩张的完整描边。
        """
        font_size = 24
        stroke_width = 6
        max_width = 240
        text = "A man survived the Hiroshima atomic bomb blast"
        font_paths = sorted(
            path
            for path in Path(utils.font_dir()).iterdir()
            if path.suffix.lower() in {".ttf", ".ttc"}
        )

        for font_path in font_paths:
            with self.subTest(font=font_path.name):
                wrapped_text, text_height = vd.wrap_text(
                    text=text,
                    max_width=max_width,
                    font=str(font_path),
                    fontsize=font_size,
                )
                line_count = wrapped_text.count("\n") + 1
                interline = int(font_size * 0.25)
                vertical_padding = int(font_size * 0.35)
                stroke_padding = stroke_width * 2 * line_count
                clip_height = int(
                    text_height
                    + vertical_padding
                    + interline * line_count
                    + stroke_padding
                )
                text_clip = vd.TextClip(
                    text=wrapped_text,
                    font=str(font_path),
                    font_size=font_size,
                    color="#FFFFFF",
                    stroke_color="#000000",
                    stroke_width=stroke_width,
                    interline=interline,
                    size=(max_width, clip_height),
                    text_align="center",
                )
                try:
                    mask = text_clip.mask.get_frame(0)
                    visible_rows, _ = vd.np.where(mask > 0.01)

                    self.assertGreater(len(visible_rows), 0)
                    self.assertLess(int(visible_rows.max()), clip_height - 1)
                finally:
                    text_clip.close()

    def test_multilingual_textclip_last_line_keeps_a_visible_bottom_margin(self):
        """
        使用 MoviePy 真实绘制多语种字幕，确保最后一行没有贴到画布底边。

        仅检查 wrap_text() 返回值会漏掉 Pillow/MoviePy 在 baseline、描边和
        行间距上的组合差异，因此这里直接读取 TextClip 的透明 mask。覆盖文本
        均由对应内置字体完整支持，包括英文、越南语、泰语、简繁中文、俄语
        和希腊语；只要可见像素触及最后一行，就说明仍存在静默裁切风险。
        """
        font_size = 60
        max_width = 360
        interline = int(font_size * 0.25)
        vertical_padding = int(font_size * 0.35)
        stroke_width = 2
        cases = (
            (
                "english_without_descenders",
                "BeVietnamPro-Bold.ttf",
                "A man survived the Hiroshima atomic bomb blast",
            ),
            (
                "vietnamese",
                "BeVietnamPro-Medium.ttf",
                "Tôi vẫn luôn tin vào một tương lai tươi sáng",
            ),
            (
                "thai",
                "Charm-Regular.ttf",
                "นี่คือข้อความสำหรับตรวจสอบบรรทัดสุดท้ายของคำบรรยาย",
            ),
            # Chinese, Cyrillic and Greek cases used to live here, covered by
            # Microsoft YaHei and STHeiti. Those fonts are proprietary and were
            # removed from the bundle, and no remaining face covers those
            # scripts - see test_bundled_fonts_script_coverage below, which
            # pins the actual limitation so it cannot regress unnoticed.
        )

        for language, font_name, text in cases:
            font_path = os.path.join(utils.font_dir(), font_name)
            with self.subTest(language=language, font=font_name):
                self.assertTrue(vd.subtitle_font_supports_text(font_path, text))
                wrapped_text, text_height = vd.wrap_text(
                    text=text,
                    max_width=max_width,
                    font=font_path,
                    fontsize=font_size,
                )
                line_count = wrapped_text.count("\n") + 1
                stroke_padding = stroke_width * 2 * line_count
                clip_height = int(
                    text_height
                    + vertical_padding
                    + interline * line_count
                    + stroke_padding
                )
                text_clip = vd.TextClip(
                    text=wrapped_text,
                    font=font_path,
                    font_size=font_size,
                    color="#FFFFFF",
                    stroke_color="#000000",
                    stroke_width=stroke_width,
                    interline=interline,
                    size=(max_width, clip_height),
                    text_align="center",
                )
                try:
                    mask = text_clip.mask.get_frame(0)
                    visible_rows, _ = vd.np.where(mask > 0.01)

                    self.assertGreater(line_count, 1)
                    self.assertGreater(len(visible_rows), 0)
                    self.assertLess(int(visible_rows.max()), clip_height - 1)
                finally:
                    text_clip.close()

    def test_rounded_subtitle_background_clip_has_transparent_corners(self):
        """
        圆角字幕背景只在用户显式开启时使用。这里直接验证生成的 RGBA
        背景具备透明圆角和半透明中心，避免后续改动把圆角效果退化成实心矩形。
        """
        clip = vd._rounded_subtitle_background_clip(
            width=120,
            height=48,
            color="#123456",
            alpha=140,
            radius=16,
        )
        try:
            frame = clip.get_frame(0)
            mask = clip.mask.get_frame(0)

            self.assertEqual(frame.shape[0:2], (48, 120))
            self.assertEqual(tuple(frame[24, 60]), (18, 52, 86))
            self.assertEqual(mask[0, 0], 0)
            self.assertGreater(mask[24, 60], 0.5)
            self.assertLess(mask[24, 60], 0.6)
        finally:
            clip.close()

    def test_get_temp_audio_dir_stays_under_system_temp_on_windows(self):
        """
        Still under the system temp directory - that is what keeps Windows
        Defender from locking the file - but no longer the shared directory
        itself, which two concurrent renders collided in.
        """
        with patch("sys.platform", "win32"):
            result = vd._get_temp_audio_dir("/some/output/dir")

        self.assertTrue(
            os.path.abspath(result).startswith(
                os.path.abspath(tempfile.gettempdir())
            ),
            f"{result} is not under {tempfile.gettempdir()}",
        )
        self.assertNotEqual(
            os.path.abspath(result), os.path.abspath(tempfile.gettempdir())
        )

    def test_get_temp_audio_dir_returns_output_dir_on_non_windows(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                with patch("sys.platform", platform):
                    result = vd._get_temp_audio_dir("/some/output/dir")
                    self.assertEqual(result, "/some/output/dir")


class TestMusicDucking(unittest.TestCase):
    """
    Pulling the music down while the narrator speaks. Without it `bgm_volume`
    has to be low enough to stay out of the way of the voice everywhere, which
    leaves the music inaudible in the gaps where it was supposed to be heard.
    """

    def _graph(self, **kwargs):
        options = {
            "voice_index": 1,
            "bgm_index": 2,
            "voice_volume": 1.0,
            "bgm_volume": 0.2,
            "video_duration": 40.0,
        }
        options.update(kwargs)
        return ";".join(vd._build_final_audio_filters(**options)[0])

    def test_the_narration_keys_the_compressor(self):
        graph = self._graph(duck_music=True)
        self.assertIn("sidechaincompress", graph)
        # The key is a copy of the voice, so it follows the pauses inside a
        # sentence rather than only the gaps between subtitles.
        self.assertIn("asplit=2[voice][duckkey]", graph)
        self.assertIn("[duckkey]sidechaincompress", graph)

    def test_the_mix_still_sums_rather_than_averages(self):
        """amix averages by default; CompositeAudioClip summed."""
        self.assertIn("normalize=0", self._graph(duck_music=True))

    def test_ducking_off_leaves_the_chain_as_it_was(self):
        graph = self._graph(duck_music=False)
        self.assertNotIn("sidechaincompress", graph)
        self.assertNotIn("asplit", graph)

    def test_no_music_means_nothing_to_duck(self):
        graph = self._graph(bgm_index=None, duck_music=True)
        self.assertNotIn("sidechaincompress", graph)
        self.assertNotIn("asplit", graph)

    def test_no_narration_means_nothing_to_duck_against(self):
        graph = self._graph(voice_index=None, duck_music=True)
        self.assertNotIn("sidechaincompress", graph)

    def test_both_sources_are_brought_to_one_format(self):
        """
        Edge TTS returns 24 kHz mono and music is usually 44.1 kHz stereo.
        sidechaincompress refuses two inputs that do not agree.
        """
        graph = self._graph(duck_music=True)
        self.assertEqual(graph.count("aformat="), 2)

    def test_the_music_still_fades_out_at_the_end(self):
        graph = self._graph(duck_music=True)
        self.assertIn("afade=t=out:st=37.000:d=3", graph)

    def test_the_narration_is_padded_so_it_cannot_shorten_the_video(self):
        self.assertIn("apad", self._graph(duck_music=True))


class TestClipFillModes(unittest.TestCase):
    """
    What fills the frame around a clip that is not the video's shape. Stock
    footage is mostly landscape and most of what this app makes is vertical, so
    this is what the sides of most finished videos look like.
    """

    def test_bars_pads_with_black(self):
        filters = vd._build_clip_fill_filters("bars", 1080, 1920)
        self.assertIn("force_original_aspect_ratio=decrease", filters)
        self.assertIn("color=black", filters)
        self.assertNotIn("gblur", filters)

    def test_crop_fills_the_frame_and_loses_the_edges(self):
        filters = vd._build_clip_fill_filters("crop", 1080, 1920)
        self.assertIn("force_original_aspect_ratio=increase", filters)
        self.assertIn("crop=1080:1920", filters)
        self.assertNotIn("pad=", filters)
        self.assertNotIn("gblur", filters)

    def test_blur_lays_the_clip_over_a_blurred_copy_of_itself(self):
        filters = vd._build_clip_fill_filters("blur", 1080, 1920)
        self.assertIn("split", filters)
        self.assertIn("gblur", filters)
        self.assertIn("overlay=", filters)
        # The backdrop fills the frame; the clip itself is fitted whole inside
        # it. Both have to be there, or one of the two halves is missing.
        self.assertIn("force_original_aspect_ratio=increase", filters)
        self.assertIn("force_original_aspect_ratio=decrease", filters)

    def test_blur_shrinks_before_blurring(self):
        """
        Blur cost grows with area and the backdrop is discarded detail anyway.
        Blurring at full size is several times slower for the same picture.
        """
        filters = vd._build_clip_fill_filters("blur", 1080, 1920)
        self.assertIn("scale=135:240", filters)

    def test_an_unset_mode_is_blurred(self):
        self.assertEqual(
            vd._build_clip_fill_filters(None, 1080, 1920),
            vd._build_clip_fill_filters("blur", 1080, 1920),
        )

    def test_an_unknown_mode_is_blurred_rather_than_failing(self):
        """A stale project file must not be able to fail a render."""
        self.assertEqual(
            vd._build_clip_fill_filters("something-else", 1080, 1920),
            vd._build_clip_fill_filters("blur", 1080, 1920),
        )

    def test_the_enum_is_accepted_as_well_as_its_value(self):
        self.assertEqual(
            vd._build_clip_fill_filters(vd.VideoFillMode.crop, 1080, 1920),
            vd._build_clip_fill_filters("crop", 1080, 1920),
        )

    def test_the_mode_reaches_the_ffmpeg_command(self):
        """The filters are only worth building if they are the ones passed."""
        captured = {}

        def _capture(command, **_kwargs):
            captured["vf"] = command[command.index("-vf") + 1]
            raise OSError("not actually running ffmpeg")

        with patch.object(vd.subprocess, "run", side_effect=_capture):
            vd._render_clip_with_ffmpeg(
                source_path="in.mp4",
                start_time=0,
                end_time=5,
                width=1080,
                height=1920,
                output_file="out.mp4",
                codec="h264_mf",
                threads=2,
                fill_mode="crop",
            )

        self.assertIn("crop=1080:1920", captured["vf"])
        self.assertNotIn("gblur", captured["vf"])


class TestCombineVideosPassesFillMode(unittest.TestCase):
    def test_the_cut_hands_the_mode_to_every_clip(self):
        """
        A setting that stops at the top of combine_videos changes nothing, and
        nothing else in the render would report it.
        """
        seen = []

        class _FakeAudioClip:
            duration = 6.0

            def close(self):
                pass

        class _FakeVideoClip:
            def __init__(self):
                self.duration = 20.0
                self.size = (1920, 1080)
                self.w, self.h = 1920, 1080

            def close(self):
                pass

        def _record(**kwargs):
            seen.append(kwargs.get("fill_mode"))
            return True

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
                patch.object(
                    vd, "_open_video_clip_quietly", return_value=_FakeVideoClip()
                ),
                patch.object(vd, "_render_clip_with_ffmpeg", side_effect=_record),
                patch.object(vd, "concat_video_clips_with_ffmpeg"),
                patch.object(vd, "delete_files"),
            ):
                vd.combine_videos(
                    combined_video_path=os.path.join(temp_dir, "c.mp4"),
                    video_paths=["a.mp4"],
                    audio_file="audio.mp3",
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    max_clip_duration=5,
                    fill_mode="bars",
                )

        self.assertTrue(seen)
        self.assertEqual(set(seen), {"bars"})


class TestTransitionsHappenBetweenClips(unittest.TestCase):
    """
    A transition is something that happens between two shots. What this used
    to do was apply an effect inside one clip and then join the clips with the
    concat demuxer, which never puts two of them on screen at once: FadeIn
    faded up from black at every cut and SlideIn slid over black rather than
    over the shot before it.
    """

    class _FakeAudioClip:
        # Five 5s clips cover this end to end and only four and a bit of them
        # once each cut takes a second, which is what makes the two counts
        # differ at all.
        duration = 24.0

        def close(self):
            pass

    class _FakeVideoClip:
        def __init__(self):
            self.duration = 30.0
            self.size = (1920, 1080)
            self.w, self.h = 1920, 1080

        def close(self):
            pass

    def _combine(self, transition_mode, **overrides):
        """Run the cut with everything below the clip stage mocked out."""
        rendered = []

        def _render(**kwargs):
            rendered.append(kwargs)
            return True

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(vd, "AudioFileClip", return_value=self._FakeAudioClip()),
                patch.object(
                    vd, "_open_video_clip_quietly", return_value=self._FakeVideoClip()
                ),
                patch.object(vd, "_render_clip_with_ffmpeg", side_effect=_render),
                patch.object(vd, "concat_video_clips_with_ffmpeg") as concat_mock,
                patch.object(vd, "delete_files"),
                patch.object(
                    vd.video_transitions, "supported_transitions",
                    return_value=frozenset({"fade", "fadeblack", "coverleft"}),
                ),
            ):
                vd.combine_videos(
                    combined_video_path=os.path.join(temp_dir, "c.mp4"),
                    video_paths=["a.mp4", "b.mp4"],
                    audio_file="audio.mp3",
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    video_transition_mode=transition_mode,
                    max_clip_duration=5,
                    **overrides,
                )
        return rendered, concat_mock

    def test_the_join_is_told_which_transition_to_use(self):
        _, concat_mock = self._combine(vd.VideoTransitionMode.fade_in)
        self.assertEqual(concat_mock.call_args.kwargs["transition"], "fade")

    def test_no_transition_leaves_the_join_alone(self):
        """The concat demuxer path stays the default; it decodes one clip at a
        time and is the cheaper of the two."""
        _, concat_mock = self._combine(None)
        self.assertIsNone(concat_mock.call_args.kwargs["transition"])

    def test_the_join_is_given_the_clip_lengths_and_the_frame(self):
        """
        An xfade offset is an absolute position on the finished timeline, and
        the graph cannot be built without knowing how long each clip runs or
        what shape the frame is.
        """
        _, concat_mock = self._combine(vd.VideoTransitionMode.slide_in)
        kwargs = concat_mock.call_args.kwargs
        self.assertEqual(len(kwargs["clip_durations"]), len(kwargs["clip_files"]))
        self.assertTrue(all(duration > 0 for duration in kwargs["clip_durations"]))
        self.assertEqual((kwargs["width"], kwargs["height"]), (1080, 1920))

    def test_a_transition_makes_the_cut_collect_more_material(self):
        """
        Every transition overlaps the two clips it joins, so the finished video
        is shorter than the clips that went into it. Measuring the sum instead
        would stop the cut a transition short of the narration - and 1s at each
        of a dozen cuts is a dozen seconds of missing picture.
        """
        without, _ = self._combine(None)
        with_transition, _ = self._combine(vd.VideoTransitionMode.fade_in)
        self.assertEqual(len(without), 5)
        self.assertEqual(len(with_transition), 6)

    def test_the_clips_themselves_are_untouched_by_the_transition(self):
        """
        Same cut, same frame filling, same speed: the only difference a
        transition makes is at the join. It used to change how the clip itself
        was rendered, which is why a transition and a plain cut did not even
        fill the frame the same way.
        """
        cut = ("source_path", "start_time", "end_time", "width", "height",
               "clip_speed", "fill_mode")
        without, _ = self._combine(None)
        with_transition, _ = self._combine(vd.VideoTransitionMode.zoom_in)
        for plain, faded in zip(without, with_transition):
            self.assertEqual(
                {key: plain[key] for key in cut},
                {key: faded[key] for key in cut},
            )

    def test_an_ffmpeg_without_xfade_falls_back_to_hard_cuts(self):
        rendered = []

        def _render(**kwargs):
            rendered.append(kwargs)
            return True

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(vd, "AudioFileClip", return_value=self._FakeAudioClip()),
                patch.object(
                    vd, "_open_video_clip_quietly", return_value=self._FakeVideoClip()
                ),
                patch.object(vd, "_render_clip_with_ffmpeg", side_effect=_render),
                patch.object(vd, "concat_video_clips_with_ffmpeg") as concat_mock,
                patch.object(vd, "delete_files"),
                patch.object(
                    vd.video_transitions, "supported_transitions",
                    return_value=frozenset(),
                ),
            ):
                vd.combine_videos(
                    combined_video_path=os.path.join(temp_dir, "c.mp4"),
                    video_paths=["a.mp4"],
                    audio_file="audio.mp3",
                    video_transition_mode=vd.VideoTransitionMode.fade_in,
                    max_clip_duration=5,
                )

        self.assertIsNone(concat_mock.call_args.kwargs["transition"])
        # And the cut is built for hard cuts, not for overlaps that will never
        # happen: a video that is a second per cut too long is as wrong as one
        # that is too short.
        self.assertEqual(len(rendered), 5)
        self.assertEqual(
            sum(
                call["end_time"] - call["start_time"] for call in rendered
            ),
            25.0,
        )


class TestJoiningClipsWithTransitions(unittest.TestCase):
    def _run_concat(self, fake_run, **overrides):
        with tempfile.TemporaryDirectory() as temp_dir:
            clip_files = [
                os.path.join(temp_dir, f"temp-clip-{index}.mp4") for index in (1, 2, 3)
            ]
            for clip_file in clip_files:
                Path(clip_file).write_bytes(b"fake")
            kwargs = {
                "clip_files": clip_files,
                "output_file": os.path.join(temp_dir, "combined.mp4"),
                "threads": 2,
                "output_dir": temp_dir,
                "transition": "fade",
                "clip_durations": [5.0, 5.0, 5.0],
                "width": 1080,
                "height": 1920,
            }
            kwargs.update(overrides)
            with (
                patch.object(vd.subprocess, "run", side_effect=fake_run),
                patch.object(vd, "_get_effective_video_codec", return_value="h264_mf"),
                patch.object(vd, "_get_ffprobe_binary", return_value=None),
            ):
                vd.concat_video_clips_with_ffmpeg(**kwargs)
            return clip_files

    def test_every_clip_becomes_its_own_input(self):
        """
        xfade needs both sides of a cut available at the same time, and the
        concat demuxer hands over one stream. That is the whole reason this
        path exists.
        """
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        clip_files = self._run_concat(fake_run)

        command = commands[-1]
        self.assertEqual([command[i + 1] for i, a in enumerate(command) if a == "-i"], clip_files)
        self.assertNotIn("concat", command)
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(graph.count("xfade=transition=fade"), 2)
        self.assertIn("-map", command)

    def test_the_output_is_still_trimmed_to_the_narration(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        self._run_concat(fake_run, max_duration=12.0)
        command = commands[-1]
        self.assertEqual(command[command.index("-t") + 1], "12.000")

    def test_the_join_states_a_bitrate_like_every_other_encode(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        self._run_concat(fake_run)
        command = commands[-1]
        self.assertEqual(
            command[command.index("-b:v") + 1], vd._INTERMEDIATE_CLIP_BITRATE
        )

    def test_a_failed_transition_falls_back_to_hard_cuts(self):
        """
        Every clip has already been encoded by the time the join runs. Hard
        cuts are a far better outcome than losing all of that work to a filter
        graph this FFmpeg would not take.
        """
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "-filter_complex" in command:
                return types.SimpleNamespace(
                    returncode=1, stdout="", stderr="No such filter: 'xfade'"
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        self._run_concat(fake_run)

        self.assertTrue(any("-filter_complex" in c for c in commands))
        demuxer_commands = [c for c in commands if "concat" in c]
        self.assertTrue(demuxer_commands, "the plain join must still have run")

    def test_one_clip_is_never_a_transition(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "temp-clip-1.mp4")
            Path(clip_file).write_bytes(b"fake")
            with (
                patch.object(vd.subprocess, "run", side_effect=fake_run),
                patch.object(vd, "_get_effective_video_codec", return_value="h264_mf"),
            ):
                vd.concat_video_clips_with_ffmpeg(
                    clip_files=[clip_file],
                    output_file=os.path.join(temp_dir, "combined.mp4"),
                    threads=2,
                    output_dir=temp_dir,
                    transition="fade",
                    clip_durations=[5.0],
                    width=1080,
                    height=1920,
                )

        self.assertFalse(any("-filter_complex" in c for c in commands))


class TestMeasuringClipsForTheTransitionOffsets(unittest.TestCase):
    """
    An xfade offset is an absolute position on the finished timeline, so a
    clip that came out a frame shorter than asked for moves every transition
    after it, and the error accumulates down the chain.
    """

    def test_the_written_length_is_preferred_to_the_requested_one(self):
        def fake_run(command, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="4.967\n", stderr="")

        with (
            patch.object(vd, "_get_ffprobe_binary", return_value="ffprobe"),
            patch.object(vd.subprocess, "run", side_effect=fake_run),
        ):
            durations = vd._measure_clip_durations(["a.mp4", "b.mp4"], [5.0, 5.0])

        self.assertEqual(durations, [4.967, 4.967])

    def test_a_clip_is_measured_once_however_often_it_is_used(self):
        """The cut repeats clip files whenever it had to loop to cover the
        narration."""
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return types.SimpleNamespace(returncode=0, stdout="5.0\n", stderr="")

        with (
            patch.object(vd, "_get_ffprobe_binary", return_value="ffprobe"),
            patch.object(vd.subprocess, "run", side_effect=fake_run),
        ):
            durations = vd._measure_clip_durations(["a.mp4"] * 4, None)

        self.assertEqual(durations, [5.0] * 4)
        self.assertEqual(len(calls), 1)

    def test_without_ffprobe_the_requested_lengths_are_used(self):
        with patch.object(vd, "_get_ffprobe_binary", return_value=None):
            self.assertEqual(
                vd._measure_clip_durations(["a.mp4", "b.mp4"], [5.0, 3.0]), [5.0, 3.0]
            )

    def test_nothing_to_measure_and_nothing_to_fall_back_on_gives_nothing(self):
        """Which is the one case where a transition chain cannot be built at
        all, and the caller joins with hard cuts instead."""
        with patch.object(vd, "_get_ffprobe_binary", return_value=None):
            self.assertIsNone(vd._measure_clip_durations(["a.mp4"], None))
            self.assertIsNone(vd._measure_clip_durations(["a.mp4"], [0.0]))


class TestTempAudioDirIsolation(unittest.TestCase):
    """
    Two renders at once must not share MoviePy's temporary audio file.

    MoviePy names it after the output file, and every task writes a
    "final-1.mp4", so pointing every render at one shared directory gave them
    one shared path: they overwrote each other's narration mid-write, and the
    first to finish deleted the file the other was still reading.
    """

    @unittest.skipUnless(sys.platform == "win32", "the shared temp dir is Windows-only")
    def test_two_tasks_do_not_share_a_temp_audio_directory(self):
        first = vd._get_temp_audio_dir(
            os.path.join(tempfile.gettempdir(), "tasks", "task-aaaa")
        )
        second = vd._get_temp_audio_dir(
            os.path.join(tempfile.gettempdir(), "tasks", "task-bbbb")
        )

        self.assertNotEqual(first, second)
        self.assertNotEqual(first, tempfile.gettempdir())
        self.assertTrue(os.path.isdir(first))
        self.assertTrue(os.path.isdir(second))

    @unittest.skipUnless(sys.platform == "win32", "the shared temp dir is Windows-only")
    def test_one_task_reuses_its_temp_audio_directory(self):
        """Stable per task, so a re-render does not leave a new directory."""
        task_dir = os.path.join(tempfile.gettempdir(), "tasks", "task-cccc")

        self.assertEqual(
            vd._get_temp_audio_dir(task_dir),
            vd._get_temp_audio_dir(task_dir),
        )

    @unittest.skipIf(sys.platform == "win32", "only the non-Windows path is asserted")
    def test_other_platforms_keep_writing_beside_the_output(self):
        self.assertEqual(vd._get_temp_audio_dir("/tmp/tasks/task-dddd"), "/tmp/tasks/task-dddd")


class TestMaterialResolutionTolerance(unittest.TestCase):
    def test_accepts_material_at_the_nominal_minimum(self):
        self.assertTrue(vd.is_material_resolution_acceptable(480, 480))

    def test_accepts_whatsapp_recompressed_portrait_clip(self):
        # WhatsApp delivers 9:16 clips as 478x850, two pixels under the
        # nominal 480 minimum. Rejecting them fails the whole task.
        self.assertTrue(vd.is_material_resolution_acceptable(478, 850))

    def test_accepts_material_exactly_at_the_tolerance_bound(self):
        bound = vd._MIN_MATERIAL_DIMENSION - vd._MIN_DIMENSION_TOLERANCE
        self.assertTrue(vd.is_material_resolution_acceptable(bound, bound))

    def test_rejects_material_just_below_the_tolerance_bound(self):
        bound = vd._MIN_MATERIAL_DIMENSION - vd._MIN_DIMENSION_TOLERANCE
        self.assertFalse(vd.is_material_resolution_acceptable(bound - 1, 850))
        self.assertFalse(vd.is_material_resolution_acceptable(850, bound - 1))

    def test_rejects_genuinely_low_resolution_material(self):
        self.assertFalse(vd.is_material_resolution_acceptable(320, 240))


class TestEncodeBitrateIsAlwaysStated(unittest.TestCase):
    """
    每一次编码都必须显式给出码率。

    h264_mf 在没有码率时会自行选择约 800 kb/s，1080x1920 的画面会被压成
    色块——成片和素材几乎看不出是同一段视频。这类回归读 diff 看不出来，
    只有看成片才会发现，所以在这里钉死。
    """

    def test_concat_command_states_a_bitrate(self):
        """拼接同样是重新编码，缺码率时这一步就足以毁掉画质。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(vd.subprocess, "run", side_effect=fake_run),
                # Pin the codec so the encoder probe does not decide what this
                # test is really about.
                patch.object(vd, "_get_effective_video_codec", return_value="h264_mf"),
            ):
                vd.concat_video_clips_with_ffmpeg(
                    clip_files=[os.path.join(temp_dir, "a.mp4")],
                    output_file=os.path.join(temp_dir, "out.mp4"),
                    threads=2,
                    output_dir=temp_dir,
                    max_duration=5.0,
                )

            command = next((c for c in commands if "-c:v" in c), [])
            self.assertIn("-b:v", command, "concat must state a video bitrate")
            self.assertEqual(
                command[command.index("-b:v") + 1], vd._INTERMEDIATE_CLIP_BITRATE
            )

    def test_clip_render_states_a_bitrate(self):
        """逐段裁剪编码是第一次编码，码率丢失会一路传递到成片。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"failed")

            with patch.object(vd.subprocess, "run", side_effect=fake_run):
                vd._render_clip_with_ffmpeg(
                    source_path=os.path.join(temp_dir, "src.mp4"),
                    start_time=0.0,
                    end_time=1.0,
                    width=1080,
                    height=1920,
                    output_file=os.path.join(temp_dir, "clip.mp4"),
                    codec="h264_mf",
                    threads=2,
                )

            command = next((c for c in commands if "-c:v" in c), [])
            self.assertIn("-b:v", command)
            self.assertEqual(
                command[command.index("-b:v") + 1], vd._INTERMEDIATE_CLIP_BITRATE
            )

    def test_final_render_states_a_bitrate(self):
        """成片是最后一次编码，也是用户唯一会看到的那一次。"""
        self.assertTrue(vd._FINAL_VIDEO_BITRATE)
        source = Path(__file__).parent.parent.parent / "app" / "services" / "video.py"
        body = source.read_text(encoding="utf-8")
        # The final write is a MoviePy call, so there is no command to inspect;
        # assert the keyword reaches it instead of the encoder default.
        self.assertIn("bitrate=_FINAL_VIDEO_BITRATE", body)


class TestFrameProgressLogger(unittest.TestCase):
    """
    烧录字幕要逐帧合成，是任务里最后一个长阶段；进度只能从 MoviePy 的
    proglog 回调里拿到，映射错了整段进度条就会静止或者倒退。
    """

    def _logger_with_total(self, callback, bar="frame_index", total=4):
        progress_logger = vd._FrameProgressLogger(callback)
        progress_logger.bars[bar] = {"index": 0, "total": total}
        return progress_logger

    def test_frame_bar_maps_to_fraction(self):
        seen = []
        progress_logger = self._logger_with_total(seen.append)

        progress_logger.bars_callback("frame_index", "index", 1)
        progress_logger.bars_callback("frame_index", "index", 4)

        self.assertEqual(seen, [0.25, 1.0])

    def test_other_bars_are_ignored(self):
        """
        写文件时音频混流会用同一个 logger 推送自己的进度条。把它也算进去，
        进度会在中途倒退回 0 附近，看起来像任务重新开始。
        """
        seen = []
        progress_logger = self._logger_with_total(seen.append)
        progress_logger.bars["chunk"] = {"index": 0, "total": 100}

        progress_logger.bars_callback("chunk", "index", 50)
        progress_logger.bars_callback("frame_index", "message", "anything")

        self.assertEqual(seen, [])

    def test_zero_total_does_not_divide(self):
        """
        proglog 会在知道总帧数之前先推一次回调，此时 total 为 0。
        """
        seen = []
        progress_logger = self._logger_with_total(seen.append, total=0)

        progress_logger.bars_callback("frame_index", "index", 1)

        self.assertEqual(seen, [])

    def test_failing_callback_does_not_break_the_write(self):
        """
        上报失败不能中断一次正在成功的写盘，否则会丢掉整段渲染成果。
        """

        def exploding(_fraction):
            raise RuntimeError("state backend unavailable")

        progress_logger = self._logger_with_total(exploding)

        progress_logger.bars_callback("frame_index", "index", 2)


if __name__ == "__main__":
    unittest.main()
