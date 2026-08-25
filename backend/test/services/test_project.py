import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import asgi
from app.services import project as project_service


class TestProjectService(unittest.TestCase):
    """
    保存的项目是渲染设置的唯一副本：任务状态默认只存在内存里，重启即清空。
    这里覆盖落盘、读回和删除的完整往返，以及会把设置悄悄改写的归一化行为。
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        patcher = patch.object(
            project_service, "projects_dir", return_value=self.directory
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_save_and_load_round_trip(self):
        record = project_service.save_project(
            title="  Morning routine  ",
            params={"video_subject": "coffee", "video_aspect": "16:9"},
            task_id="task-1",
        )

        self.assertEqual(record["title"], "Morning routine")
        self.assertEqual(record["task_id"], "task-1")

        loaded = project_service.load_project(record["id"])
        self.assertEqual(loaded, record)
        self.assertEqual(loaded["params"]["video_subject"], "coffee")

    def test_saved_params_are_json_values_the_api_accepts(self):
        """
        VideoParams 里有多个枚举字段。直接落盘枚举对象会写出无法回传的内容，
        项目就只能看不能用；这里固定“存的是字符串值”这一前提。
        """
        record = project_service.save_project(
            title="Enums",
            params={"video_subject": "x", "video_concat_mode": "sequential"},
        )

        raw = json.loads(
            open(
                os.path.join(self.directory, f"{record['id']}.json"), encoding="utf-8"
            ).read()
        )
        self.assertEqual(raw["params"]["video_concat_mode"], "sequential")
        self.assertEqual(raw["params"]["video_aspect"], "9:16")

    def test_transient_run_fields_are_not_saved(self):
        """
        ``custom_audio_file`` 指向某一次任务目录里的文件。原样存进项目后，
        下次加载会用旧旁白覆盖新视频，或者直接因为文件不存在而失败。
        """
        record = project_service.save_project(
            title="Has audio",
            params={"video_subject": "x", "custom_audio_file": "/tmp/audio.mp3"},
        )
        self.assertNotIn("custom_audio_file", record["params"])

    def test_clip_list_is_stored_in_order(self):
        """
        重新编辑的意义在于拿回原来的片段。顺序本身就是用户的编辑成果，
        保存时不能重排。
        """
        clips = ["/tasks/t/vid-c.mp4", "/tasks/t/vid-a.mp4", "/tasks/t/vid-b.mp4"]
        record = project_service.save_project(
            title="p", params={"video_subject": "x"}, materials=clips
        )
        self.assertEqual(record["materials"], clips)
        self.assertEqual(project_service.load_project(record["id"])["materials"], clips)

    def test_clip_list_drops_blanks_and_caps_length(self):
        record = project_service.save_project(
            title="p",
            params={"video_subject": "x"},
            materials=["  /a.mp4  ", "", "   ", None, 5]
            + [f"/clip-{i}.mp4" for i in range(project_service.MAX_MATERIALS + 20)],
        )
        self.assertEqual(record["materials"][0], "/a.mp4")
        self.assertEqual(len(record["materials"]), project_service.MAX_MATERIALS)

    def test_clip_list_must_be_a_list(self):
        with self.assertRaises(ValueError):
            project_service.save_project(
                title="p", params={"video_subject": "x"}, materials="/a.mp4"
            )

    def test_summary_reports_the_clip_count(self):
        project_service.save_project(
            title="p", params={"video_subject": "x"}, materials=["/a.mp4", "/b.mp4"]
        )
        self.assertEqual(project_service.list_projects()[0]["clip_count"], 2)

    def test_project_saved_before_clips_were_stored_reads_as_empty(self):
        """旧项目文件没有 materials 键，读取方不应为此写兼容分支。"""
        record = project_service.save_project(title="p", params={"video_subject": "x"})
        path = os.path.join(self.directory, f"{record['id']}.json")
        stored = json.loads(open(path, encoding="utf-8").read())
        del stored["materials"]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(stored, handle)

        self.assertEqual(project_service.load_project(record["id"])["materials"], [])
        self.assertEqual(project_service.list_projects()[0]["clip_count"], 0)

    def test_update_keeps_clips_when_they_are_not_sent(self):
        """
        改个名字不该丢掉片段列表。省略即保持原值，只有显式传空列表才清空。
        """
        clips = ["/a.mp4", "/b.mp4"]
        record = project_service.save_project(
            title="p", params={"video_subject": "x"}, materials=clips
        )

        renamed = project_service.update_project(record["id"], title="renamed")
        self.assertEqual(renamed["materials"], clips)

        cleared = project_service.update_project(record["id"], materials=[])
        self.assertEqual(cleared["materials"], [])

    def test_update_replaces_the_clip_list_and_task(self):
        record = project_service.save_project(
            title="p", params={"video_subject": "x"}, materials=["/a.mp4"], task_id="t1"
        )
        updated = project_service.update_project(
            record["id"], task_id="t2", materials=["/b.mp4", "/c.mp4"]
        )
        self.assertEqual(updated["task_id"], "t2")
        self.assertEqual(updated["materials"], ["/b.mp4", "/c.mp4"])

    def test_untitled_project_gets_a_default_name(self):
        record = project_service.save_project(title="   ", params={"video_subject": "x"})
        self.assertEqual(record["title"], project_service.DEFAULT_TITLE)

    def test_title_is_capped(self):
        record = project_service.save_project(
            title="t" * 500, params={"video_subject": "x"}
        )
        self.assertEqual(len(record["title"]), project_service.MAX_TITLE_LENGTH)

    def test_invalid_params_are_rejected_before_writing(self):
        with self.assertRaises(ValueError):
            project_service.save_project(title="bad", params={"no_subject": True})
        with self.assertRaises(ValueError):
            project_service.save_project(title="bad", params="not an object")
        self.assertEqual(os.listdir(self.directory), [])

    def test_project_id_pattern_guards_path_building(self):
        for bad_id in ("../../etc/passwd", "not-a-uuid", "", "a" * 31):
            with self.subTest(project_id=bad_id):
                self.assertFalse(project_service.is_valid_project_id(bad_id))
                with self.assertRaises(ValueError):
                    project_service._project_file(bad_id)

    def test_list_is_newest_first_and_skips_foreign_files(self):
        older = project_service.save_project(title="older", params={"video_subject": "a"})
        newer = project_service.save_project(title="newer", params={"video_subject": "b"})
        # 手工制造一条比另一条更早的记录，避免依赖两次写入之间的真实时间差。
        stored = project_service.load_project(older["id"])
        stored["created_at"] = "2000-01-01T00:00:00+00:00"
        project_service._write_record(stored)

        with open(os.path.join(self.directory, "notes.txt"), "w") as handle:
            handle.write("not a project")
        with open(os.path.join(self.directory, "broken.json"), "w") as handle:
            handle.write("{ not json")

        listed = project_service.list_projects()
        self.assertEqual([item["id"] for item in listed], [newer["id"], older["id"]])
        self.assertEqual(listed[0]["video_subject"], "b")
        # 列表只给元数据，完整设置留给按 id 读取的接口。
        self.assertNotIn("params", listed[0])

    def test_unreadable_project_file_is_skipped_not_fatal(self):
        good = project_service.save_project(title="good", params={"video_subject": "a"})
        broken_id = "f" * 32
        with open(
            os.path.join(self.directory, f"{broken_id}.json"), "w", encoding="utf-8"
        ) as handle:
            handle.write("{ truncated")

        listed = project_service.list_projects()
        self.assertEqual([item["id"] for item in listed], [good["id"]])
        self.assertIsNone(project_service.load_project(broken_id))

    def test_update_renames_without_touching_settings(self):
        record = project_service.save_project(
            title="before", params={"video_subject": "coffee"}
        )
        updated = project_service.update_project(record["id"], title="after")

        self.assertEqual(updated["title"], "after")
        self.assertEqual(updated["params"], record["params"])
        self.assertEqual(updated["created_at"], record["created_at"])

    def test_update_replaces_settings(self):
        record = project_service.save_project(
            title="p", params={"video_subject": "coffee", "font_size": 60}
        )
        updated = project_service.update_project(
            record["id"], params={"video_subject": "tea", "font_size": 80}
        )

        self.assertEqual(updated["title"], "p")
        self.assertEqual(updated["params"]["video_subject"], "tea")
        self.assertEqual(updated["params"]["font_size"], 80)

    def test_update_and_delete_report_a_missing_project(self):
        missing = "0" * 32
        self.assertIsNone(project_service.update_project(missing, title="x"))
        self.assertFalse(project_service.delete_project(missing))

    def test_delete_removes_the_file(self):
        record = project_service.save_project(title="p", params={"video_subject": "x"})
        self.assertTrue(project_service.delete_project(record["id"]))
        self.assertEqual(os.listdir(self.directory), [])
        self.assertIsNone(project_service.load_project(record["id"]))

    def test_write_leaves_no_temporary_file_behind(self):
        record = project_service.save_project(title="p", params={"video_subject": "x"})
        self.assertEqual(os.listdir(self.directory), [f"{record['id']}.json"])


class TestProjectController(unittest.TestCase):
    """项目接口是前端唯一的持久化入口，状态码和信封形状都要稳定。"""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        patcher = patch.object(
            project_service, "projects_dir", return_value=self.directory
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.client = TestClient(asgi.app)

    def _create(self, title="Project", subject="coffee"):
        response = self.client.post(
            "/api/v1/projects",
            json={"title": title, "params": {"video_subject": subject}},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]["project"]

    def test_empty_listing_still_returns_the_projects_key(self):
        """
        响应信封会丢掉假值 data。列表必须包在字典里，否则前端拿到 undefined
        再去 .map 就会白屏。
        """
        response = self.client.get("/api/v1/projects")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {"projects": []})

    def test_create_list_get_delete_round_trip(self):
        created = self._create(title="Morning routine")

        listing = self.client.get("/api/v1/projects").json()["data"]["projects"]
        self.assertEqual([item["id"] for item in listing], [created["id"]])

        loaded = self.client.get(f"/api/v1/projects/{created['id']}")
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(
            loaded.json()["data"]["project"]["params"]["video_subject"], "coffee"
        )

        self.assertEqual(
            self.client.delete(f"/api/v1/projects/{created['id']}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{created['id']}").status_code, 404
        )

    def test_malformed_id_is_rejected_with_400(self):
        for bad_id in ("nope", "a" * 31, "A" * 32, "0" * 33):
            with self.subTest(project_id=bad_id):
                self.assertEqual(
                    self.client.get(f"/api/v1/projects/{bad_id}").status_code, 400
                )
                self.assertEqual(
                    self.client.delete(f"/api/v1/projects/{bad_id}").status_code, 400
                )

    def test_path_traversal_never_reaches_the_project_directory(self):
        """
        编码斜杠会在 HTTP 层被还原成多段路径，请求因此根本匹配不到本路由，
        由框架返回 404/405 而不是进入处理函数。这里固定的是结果：拒绝，
        且磁盘不受影响。
        """
        for bad_id in ("..%2F..%2Fconfig.toml", "..%2Ftasks"):
            with self.subTest(project_id=bad_id):
                self.assertGreaterEqual(
                    self.client.get(f"/api/v1/projects/{bad_id}").status_code, 400
                )
                self.assertGreaterEqual(
                    self.client.delete(f"/api/v1/projects/{bad_id}").status_code, 400
                )
        self.assertEqual(os.listdir(self.directory), [])

    def test_params_the_render_api_would_reject_are_refused(self):
        response = self.client.post(
            "/api/v1/projects", json={"title": "bad", "params": {"font_size": 10}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(os.listdir(self.directory), [])

    def test_update_requires_something_to_change(self):
        created = self._create()
        self.assertEqual(
            self.client.put(f"/api/v1/projects/{created['id']}", json={}).status_code,
            400,
        )

    def test_update_renames(self):
        created = self._create(title="before")
        response = self.client.put(
            f"/api/v1/projects/{created['id']}", json={"title": "after"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["project"]["title"], "after")

    def test_update_and_delete_report_404_for_a_missing_project(self):
        missing = "0" * 32
        self.assertEqual(
            self.client.put(f"/api/v1/projects/{missing}", json={"title": "x"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"/api/v1/projects/{missing}").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
