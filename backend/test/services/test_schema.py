import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import VideoAspect, VideoParams


class TestVideoAspect(unittest.TestCase):
    def test_to_resolution_known_aspects(self):
        self.assertEqual(VideoAspect.landscape.to_resolution(), (1920, 1080))
        self.assertEqual(VideoAspect.portrait.to_resolution(), (1080, 1920))
        self.assertEqual(VideoAspect.square.to_resolution(), (1080, 1080))

    def test_to_resolution_rejects_unsupported_value(self):
        with self.assertRaises(ValueError):
            VideoAspect.to_resolution("4:5")


class TestVideoParams(unittest.TestCase):
    def test_rejects_non_positive_generation_counts(self):
        for field_name, invalid_values in (
            # 镜头时长的 0 是“随机长度”的哨兵值，不是非法输入；
            # 负数和 None 仍然没有任何含义。
            ("video_clip_duration", (-1, None)),
            ("video_count", (0, -1, None)),
        ):
            for value in invalid_values:
                with self.subTest(field_name=field_name, value=value):
                    with self.assertRaises(ValidationError):
                        VideoParams(video_subject="Coffee", **{field_name: value})

    def test_accepts_positive_generation_counts(self):
        params = VideoParams(
            video_subject="Coffee", video_clip_duration=1, video_count=1
        )

        self.assertEqual(params.video_clip_duration, 1)
        self.assertEqual(params.video_count, 1)

    def test_scene_length_defaults_to_varied(self):
        """不传镜头时长时默认随机（5/10/15 秒），而不是固定 5 秒。"""
        params = VideoParams(video_subject="Coffee")

        self.assertEqual(params.video_clip_duration, 0)
        self.assertEqual(VideoParams(video_subject="Coffee", video_clip_duration=0).video_clip_duration, 0)


if __name__ == "__main__":
    unittest.main()
