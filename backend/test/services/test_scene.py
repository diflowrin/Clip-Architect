import random
import unittest

from app.services import scene


class TestSceneLength(unittest.TestCase):
    """场景时长计划：默认随机 5/10/15 秒，显式设置则固定。"""

    def test_a_pinned_length_is_used_for_every_scene(self):
        self.assertFalse(scene.is_varied(8))
        self.assertEqual(
            [scene.next_scene_duration(8) for _ in range(5)], [8, 8, 8, 8, 8]
        )
        self.assertEqual(scene.longest_scene_duration(8), 8)
        self.assertEqual(scene.minimum_material_duration(8), 8)

    def test_zero_means_varied_and_only_draws_from_the_choices(self):
        self.assertTrue(scene.is_varied(0))
        rng = random.Random(1)
        draws = {scene.next_scene_duration(0, rng=rng) for _ in range(200)}
        self.assertEqual(draws, set(scene.SCENE_DURATION_CHOICES))

    def test_a_long_scene_is_kept_out_of_a_short_remainder(self):
        """
        二十秒的视频里放十五秒的镜头，等于一个镜头占掉四分之三片长。
        剩余时长很短时只允许最短镜头，避免单个镜头吞掉整条视频。
        """
        rng = random.Random(2)
        draws = {scene.next_scene_duration(0, remaining=3, rng=rng) for _ in range(100)}
        self.assertEqual(draws, {min(scene.SCENE_DURATION_CHOICES)})

    def test_one_scene_may_run_past_the_remainder_to_finish_the_video(self):
        """
        只允许“不超过剩余时长”的镜头，会让每条视频的结尾都被拆成一串
        最短镜头，下载数量又回到原来的水平。允许刚好越过剩余时长的那一档，
        八秒的缺口就能用一个十秒镜头收尾。
        """
        rng = random.Random(3)
        draws = {scene.next_scene_duration(0, remaining=8, rng=rng) for _ in range(100)}
        self.assertEqual(draws, {5, 10})

    def test_a_malformed_setting_falls_back_to_the_varied_plan(self):
        self.assertTrue(scene.is_varied(None))
        self.assertTrue(scene.is_varied("not a number"))
        self.assertIn(
            scene.next_scene_duration(None), list(scene.SCENE_DURATION_CHOICES)
        )


class TestMaterialCeiling(unittest.TestCase):
    """下载数量上限：按片长等比例封顶，且不推翻用户显式设定的镜头长度。"""

    def test_a_minute_of_video_may_download_seven_clips(self):
        self.assertEqual(scene.max_material_count(60), 7)
        self.assertEqual(scene.max_material_count(120), 14)

    def test_even_a_very_short_video_gets_more_than_one_clip(self):
        self.assertEqual(scene.max_material_count(4), 2)
        self.assertEqual(scene.max_material_count(0), 2)

    def test_a_pinned_scene_length_is_not_overruled_by_the_ceiling(self):
        """
        选择“每个镜头 5 秒”就是选择每分钟十二个镜头。上限只为默认的
        随机计划兜底，不能反过来决定用户显式要求的成片节奏。
        """
        self.assertEqual(scene.max_material_count(60, clip_duration=5), 13)
        self.assertEqual(scene.max_material_count(60, clip_duration=15), 7)

    def test_the_varied_plan_stays_under_the_ceiling_for_a_long_narration(self):
        """
        69 秒旁白曾经下载 15 个片段。用随机计划模拟同一条视频，数量必须
        落在上限之内，而且明显少于每 5 秒一个片段的老逻辑。
        """
        for seed in range(50):
            rng = random.Random(seed)
            target = 69.0
            ceiling = scene.max_material_count(target)
            total = 0.0
            count = 0
            while total <= target and count < ceiling:
                total += scene.next_scene_duration(0, remaining=target - total, rng=rng)
                count += 1
            self.assertLessEqual(count, ceiling)
            self.assertLess(count, 15)


if __name__ == "__main__":
    unittest.main()
