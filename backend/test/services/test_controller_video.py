import asyncio
import json
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.v1 import video as video_controller
from app.models import const
from app.models.exception import HttpException
from app.models.schema import TaskDeletionResponse, TaskListResponse, TaskQueryResponse
from app.services import material_upload
from app.services import state as sm
from app.services.state import MemoryState
from app.utils import utils


class TestVideoControllerHelpers(unittest.TestCase):
    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_sanitize_upload_filename_removes_client_path(self):
        """Windows 和 POSIX 客户端路径都只能保留最后一段安全文件名。"""
        for filename, expected in (
            (r"C:\videos\clip.MOV", "clip.MOV"),
            ("../../images/photo.png", "photo.png"),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(
                    video_controller._sanitize_upload_filename(filename, "request-123"),
                    expected,
                )

    def test_fastapi_startup_recovers_interrupted_cross_posts(self):
        """API 进程启动时必须执行一次发布遗留状态恢复。"""
        from app.services import task as task_service

        with patch.object(task_service, "recover_interrupted_cross_posts") as recover:

            async def run_lifespan():
                async with asgi.application_lifespan(asgi.app):
                    pass

            asyncio.run(run_lifespan())

        recover.assert_called_once_with()

    def test_sanitize_upload_filename_rejects_empty_name(self):
        """空文件名和目录占位符不能进入服务端存储路径。"""
        for filename in ("", ".", "..", "/"):
            with self.subTest(filename=filename):
                with self.assertRaises(HttpException) as raised:
                    video_controller._sanitize_upload_filename(filename, "request-123")
                self.assertEqual(raised.exception.status_code, 400)

    def test_resolve_path_maps_missing_and_unsafe_files(self):
        """不存在文件返回 404，目录穿越等非法路径返回 403。"""
        for error, expected_status in (
            ("file does not exist", 404),
            ("path escapes base directory", 403),
        ):
            with self.subTest(error=error):
                with patch.object(
                    video_controller.file_security,
                    "resolve_path_within_directory",
                    side_effect=ValueError(error),
                ):
                    with self.assertRaises(HttpException) as raised:
                        video_controller._resolve_path_within_directory(
                            "/tasks", "../secret", "request-123"
                        )
                self.assertEqual(raised.exception.status_code, expected_status)

    def test_parse_byte_range_supports_common_player_requests(self):
        """播放器常见的闭区间、开放区间和后缀区间都应得到准确边界。"""
        cases = (
            (None, (0, 9)),
            ("bytes=2-5", (2, 5)),
            ("bytes=4-", (4, 9)),
            ("bytes=-4", (6, 9)),
            ("bytes=2-50", (2, 9)),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(
                    video_controller._parse_byte_range(header, 10, "request-123"),
                    expected,
                )

    def test_parse_byte_range_rejects_malformed_or_out_of_bounds_requests(self):
        """非法 Range 必须返回 416，不能因 split 或 int 转换异常变成 500。"""
        invalid_headers = (
            "items=0-1",
            "bytes=",
            "bytes=10-",
            "bytes=5-2",
            "bytes=0-1,3-4",
        )
        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(HttpException) as raised:
                    video_controller._parse_byte_range(header, 10, "request-123")
                self.assertEqual(raised.exception.status_code, 416)


class TestVideoControllerTasks(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def test_create_task_queues_requested_pipeline_stage(self):
        """创建任务应持久化初始状态，并把原请求模型与停止阶段交给队列。"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm.state, "update_task") as update_task,
            patch.object(video_controller.task_manager, "add_task") as add_task,
        ):
            response = video_controller.create_task(
                self._request(), body, stop_at="audio"
            )

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"]["task_id"], "task-123")
        self.assertEqual(response["data"]["request_id"], "request-123")
        update_task.assert_called_once_with("task-123")
        add_task.assert_called_once_with(
            video_controller.tm.start,
            task_id="task-123",
            params=body,
            stop_at="audio",
        )

    def test_create_task_removes_state_when_queue_is_full(self):
        """队列已满时必须回滚刚创建的状态，并向调用方返回 429。"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm.state, "update_task"),
            patch.object(
                video_controller.task_manager,
                "add_task",
                side_effect=TaskQueueFullError("queue full"),
            ),
            patch.object(video_controller.sm.state, "delete_task") as delete_task,
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.create_task(self._request(), body, stop_at="video")

        self.assertEqual(raised.exception.status_code, 429)
        delete_task.assert_called_once_with("task-123")

    def test_create_task_removes_state_when_scheduler_fails(self):
        """调度器未能接管任务时，不能留下永远处于 processing 的状态。"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}
        scheduling_error = RuntimeError("can't start new thread")
        state = sm.MemoryState()

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm, "state", state),
            patch.object(
                video_controller.task_manager,
                "add_task",
                side_effect=scheduling_error,
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                video_controller.create_task(self._request(), body, stop_at="video")

        self.assertIs(raised.exception, scheduling_error)
        self.assertIsNone(state.get_task("task-123"))

    def test_get_all_tasks_preserves_pagination(self):
        """任务列表响应必须包含状态层返回的总数和请求分页参数。"""
        with patch.object(
            video_controller.sm.state,
            "get_all_tasks",
            return_value=([{"id": "task-1", "cross_post_owner": "internal"}], 21),
        ) as get_all:
            response = video_controller.get_all_tasks(
                self._request(), page=2, page_size=10
            )

        self.assertEqual(
            response["data"],
            {
                "tasks": [{"id": "task-1"}],
                "total": 21,
                "page": 2,
                "page_size": 10,
            },
        )
        get_all.assert_called_once_with(2, 10)

    def test_task_query_returns_relative_url_without_mutating_state(self):
        """
        endpoint 未配置时应返回相对任务 URL，且不能把展示用 URL 回写到状态，
        否则后续请求可能基于已改写数据重复拼接路径。
        """
        task_id = "controller-task-url"
        task_dir = utils.task_dir(task_id)
        video_path = os.path.join(task_dir, "final-1.mp4")
        Path(video_path).write_bytes(b"fake-video")

        try:
            sm.state.update_task(
                task_id,
                state=const.TASK_STATE_COMPLETE,
                videos=[video_path],
                combined_videos=[video_path],
                cross_post_owner="localhost:123:internal",
            )
            with patch.dict(config.app, {"endpoint": ""}):
                response = video_controller.get_task(
                    self._request(), task_id=task_id, query=MagicMock()
                )

            self.assertEqual(
                response["data"]["videos"],
                [f"/tasks/{task_id}/final-1.mp4"],
            )
            self.assertNotIn("cross_post_owner", response["data"])
            self.assertIn("cross_post_owner", sm.state.get_task(task_id))
            self.assertEqual(sm.state.get_task(task_id)["videos"], [video_path])
        finally:
            sm.state.delete_task(task_id)
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_task_query_preserves_structured_failure_details(self):
        """失败阶段和错误信息必须通过任务查询接口原样返回。"""
        failed_task = {
            "task_id": "failed-task",
            "state": const.TASK_STATE_FAILED,
            "progress": 30,
            "failed_stage": "audio",
            "error": "TTS request timed out",
        }

        with patch.object(
            video_controller.sm.state,
            "get_task",
            return_value=failed_task,
        ):
            response = video_controller.get_task(
                self._request(), task_id="failed-task", query=MagicMock()
            )

        self.assertEqual(response["data"], failed_task)

    def test_task_query_schema_documents_success_and_failure_states(self):
        """OpenAPI 模型示例必须覆盖发布成功和生成失败两种状态。"""
        examples = TaskQueryResponse.model_json_schema()["examples"]

        self.assertEqual(examples[0]["data"]["cross_post_state"], "complete")
        self.assertEqual(examples[1]["data"]["failed_stage"], "audio")
        self.assertTrue(examples[1]["data"]["error"])

        task_data_schema = TaskQueryResponse.model_json_schema()["$defs"][
            "TaskStatusData"
        ]
        self.assertIn("failed_stage", task_data_schema["properties"])
        self.assertIn("cross_post_state", task_data_schema["properties"])

        list_schema = TaskListResponse.model_json_schema()
        self.assertIn("TaskListData", list_schema["$defs"])
        self.assertIn("TaskStatusData", list_schema["$defs"])

    def test_task_deletion_schema_defines_null_data_contract(self):
        """TaskDeletionResponse 的 OpenAPI 架构必须将 data 显式声明为 null 类型。"""
        schema = TaskDeletionResponse.model_json_schema()
        data_property = schema["properties"]["data"]

        self.assertEqual(data_property.get("type"), "null")
        self.assertIsNone(data_property.get("default"))

    def test_delete_rejects_generation_and_cross_posting_tasks(self):
        """生成中和发布中的任务都在读取目录，删除接口必须返回 409。"""
        busy_tasks = (
            {
                "task_id": "generating-task",
                "state": const.TASK_STATE_PROCESSING,
                "progress": 30,
            },
            {
                "task_id": "publishing-task",
                "state": const.TASK_STATE_COMPLETE,
                "progress": 100,
                "cross_post_state": const.CROSS_POST_STATE_PROCESSING,
            },
        )

        for task in busy_tasks:
            with (
                self.subTest(task_id=task["task_id"]),
                patch.object(
                    video_controller.sm.state,
                    "get_task",
                    return_value=task,
                ),
                patch.object(video_controller.sm.state, "delete_task") as delete_task,
            ):
                with self.assertRaises(HttpException) as raised:
                    video_controller.delete_video(
                        self._request(), task_id=task["task_id"]
                    )

                self.assertEqual(raised.exception.status_code, 409)
                delete_task.assert_not_called()

    def test_delete_allows_completed_task(self):
        """普通已完成任务仍应保持原有删除行为。"""
        completed_task = {
            "task_id": "completed-task",
            "state": const.TASK_STATE_COMPLETE,
            "progress": 100,
            "cross_post_state": const.CROSS_POST_STATE_COMPLETE,
        }

        with (
            patch.object(
                video_controller.sm.state,
                "get_task",
                return_value=completed_task,
            ),
            patch.object(
                video_controller.utils,
                "task_dir",
                return_value="/tmp/mpt-completed-task-test",
            ),
            patch.object(video_controller.os.path, "exists", return_value=False),
            patch.object(video_controller.sm.state, "delete_task") as delete_task,
        ):
            response = video_controller.delete_video(
                self._request(), task_id="completed-task"
            )

        self.assertEqual(response["status"], 200)
        delete_task.assert_called_once_with("completed-task")

    def test_get_and_delete_missing_task_return_404(self):
        """查询或删除未知任务都应返回一致的 404，而不是空成功响应。"""
        with patch.object(video_controller.sm.state, "get_task", return_value=None):
            for operation in (
                lambda: video_controller.get_task(
                    self._request(), task_id="missing", query=MagicMock()
                ),
                lambda: video_controller.delete_video(
                    self._request(), task_id="missing"
                ),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(HttpException) as raised:
                        operation()
                    self.assertEqual(raised.exception.status_code, 404)


class TestVideoControllerDeleteHTTP(unittest.TestCase):
    """DELETE /api/v1/tasks/{task_id} 的真实 HTTP 级回归测试。"""

    def setUp(self):
        self.original_app_config = dict(config.app)
        # 这些用例只验证任务删除协议；鉴权行为由独立测试覆盖。
        config.app["api_key"] = ""
        self.client = TestClient(asgi.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def _seed_completed_task(self, task_id: str) -> str:
        """创建一个已完成的任务，返回其存储目录路径。"""

        task_dir = utils.task_dir(task_id)
        video_path = os.path.join(task_dir, "final-1.mp4")
        Path(video_path).write_bytes(b"fake-video")
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            videos=[video_path],
            combined_videos=[video_path],
            cross_post_state=const.CROSS_POST_STATE_COMPLETE,
        )
        return task_dir

    def test_delete_completed_task_returns_success_response(self):
        """成功删除应返回 200, 且响应体必须是控制器的真实输出 (status/message/data)"""

        task_id = "http-delete-success-task"
        task_dir = self._seed_completed_task(task_id)

        try:
            response = self.client.delete(f"/api/v1/tasks/{task_id}")
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)
            sm.state.delete_task(task_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": 200, "message": "success", "data": None},
        )

    def test_deleted_task_lookup_returns_404(self):
        """删除后再次查询必须返回 404，确认任务确实从状态存储中移除，
        而不只是删除接口本身的响应格式正确。"""

        task_id = "http-delete-lookup-task"
        task_dir = self._seed_completed_task(task_id)

        try:
            delete_response = self.client.delete(f"/api/v1/tasks/{task_id}")
            self.assertEqual(delete_response.status_code, 200)

            lookup_response = self.client.get(f"/api/v1/tasks/{task_id}")
        finally:
            # 任务此时应已被删除；只清理可能残留的目录。
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(lookup_response.status_code, 404)


class TestVideoControllerFiles(unittest.TestCase):
    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_upload_video_material_validates_complete_extension(self):
        """大写合法扩展名应接受，无点号伪扩展名应拒绝。"""
        upload = SimpleNamespace(
            filename=r"C:\videos\clip.MOV",
            file=BytesIO(b"video"),
        )
        with patch.object(
            material_upload,
            "save_material_upload",
            return_value="4fca18fce7344f3aa824777a40d45c8c.mov",
        ) as save_material:
            response = video_controller.upload_video_material_file(
                self._request(), upload
            )

        self.assertEqual(
            response["data"]["file"],
            "4fca18fce7344f3aa824777a40d45c8c.mov",
        )
        save_material.assert_called_once_with("clip.MOV", upload.file)

        invalid_upload = SimpleNamespace(
            filename="photojpg",
            file=BytesIO(b"not-an-image"),
        )
        with patch.object(
            material_upload,
            "save_material_upload",
            side_effect=material_upload.MaterialUploadError("unsupported format"),
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.upload_video_material_file(
                    self._request(), invalid_upload
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_upload_video_material_maps_service_failure_to_stable_500(self):
        upload = SimpleNamespace(filename="clip.mp4", file=BytesIO(b"video"))
        with patch.object(
            material_upload,
            "save_material_upload",
            side_effect=material_upload.MaterialServiceError(
                "C:\\sensitive\\storage is unavailable"
            ),
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.upload_video_material_file(self._request(), upload)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertNotIn("sensitive", raised.exception.message)

    def test_stream_video_returns_requested_bytes(self):
        """Range 响应的正文和 Content-Range 必须与计算出的区间一致。"""

        async def consume(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "clip.mp4").write_bytes(b"0123456789")
            with patch.object(
                video_controller.utils,
                "task_dir",
                return_value=temp_dir,
            ):
                response = asyncio.run(
                    video_controller.stream_video(
                        self._request("bytes=2-5"), "clip.mp4"
                    )
                )
                body = asyncio.run(consume(response))

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(body, b"2345")

    def test_download_video_uses_resolved_file(self):
        """下载响应应使用白名单目录解析后的真实路径和原始文件名。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir, "final-1.mp4")
            video_path.write_bytes(b"video")
            with patch.object(
                video_controller.utils,
                "task_dir",
                return_value=temp_dir,
            ):
                response = asyncio.run(
                    video_controller.download_video(self._request(), "final-1.mp4")
                )

        # macOS 的 /var 是 /private/var 符号链接，安全解析会返回真实路径。
        self.assertEqual(response.path, os.path.realpath(video_path))
        self.assertEqual(response.filename, "final-1.mp4")
        self.assertEqual(response.media_type, "video/mp4")


class TestTaskResponsePaths(unittest.TestCase):
    """
    列表接口和单任务接口必须返回同样的可访问链接。

    此前只有单任务接口做了路径转换，列表接口直接返回磁盘绝对路径，
    作品库里每一个“Open video”都会失败，报的还是 webview 的
    “not allowed to open url”，完全看不出问题在服务端。
    """

    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def setUp(self):
        self.tasks_root = tempfile.mkdtemp()
        self.task_dir = os.path.join(self.tasks_root, "task-1")
        os.makedirs(self.task_dir)
        self.final = os.path.join(self.task_dir, "final-1.mp4")
        Path(self.final).write_bytes(b"video")
        self.task = {
            "task_id": "task-1",
            "state": const.TASK_STATE_COMPLETE,
            "progress": 100,
            "videos": [self.final],
        }

    def tearDown(self):
        shutil.rmtree(self.tasks_root, ignore_errors=True)

    def test_list_and_single_task_agree(self):
        with (
            patch.object(
                video_controller.utils, "task_dir", return_value=self.tasks_root
            ),
            patch.object(
                video_controller.sm.state,
                "get_all_tasks",
                return_value=([dict(self.task)], 1),
            ),
            patch.object(
                video_controller.sm.state, "get_task", return_value=dict(self.task)
            ),
        ):
            listed = video_controller.get_all_tasks(self._request())
            single = video_controller.get_task(self._request(), task_id="task-1")

        from_list = listed["data"]["tasks"][0]["videos"]
        from_single = single["data"]["videos"]
        self.assertEqual(from_list, from_single)
        self.assertEqual(from_list, ["/tasks/task-1/final-1.mp4"])
        self.assertNotIn(
            os.sep,
            from_list[0].replace("/", ""),
            "an absolute host path must never reach the client",
        )


class TestDownloadedMaterials(unittest.TestCase):
    """
    下载缓存不设上限，几次生成就能到 GB 级别，用户必须能看见并删除它们。
    """

    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.cache = os.path.join(self.temp_root, "cache_videos")
        os.makedirs(self.cache)
        Path(self.cache, "vid-a.mp4").write_bytes(b"a" * 100)
        Path(self.cache, "vid-b.mp4").write_bytes(b"b" * 250)
        Path(self.cache, "notes.txt").write_bytes(b"not a video")
        Path(self.temp_root, "outside.mp4").write_bytes(b"outside")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_lists_only_videos_with_size_and_total(self):
        with patch.object(
            video_controller.utils, "storage_dir", return_value=self.cache
        ):
            response = video_controller.list_downloaded_materials(self._request())

        data = response["data"]
        names = sorted(entry["name"] for entry in data["files"])
        self.assertEqual(names, ["vid-a.mp4", "vid-b.mp4"])
        self.assertEqual(data["total_size"], 350)
        for entry in data["files"]:
            # Absolute host paths must not leave the API, same as every other
            # listing endpoint.
            self.assertNotIn(os.sep, entry["file"])

    def test_delete_removes_the_file_from_disk(self):
        target = os.path.join(self.cache, "vid-a.mp4")
        with patch.object(
            video_controller.utils, "storage_dir", return_value=self.cache
        ):
            response = video_controller.delete_downloaded_material(
                self._request(), file_name="vid-a.mp4"
            )

        self.assertEqual(response["status"], 200)
        self.assertFalse(os.path.exists(target), "the file must actually be gone")
        self.assertTrue(os.path.exists(os.path.join(self.cache, "vid-b.mp4")))

    def test_thumbnail_command_writes_a_jpeg_named_file(self):
        """
        ffmpeg 按扩展名选择封装格式：临时文件若以 .tmp 结尾会直接失败，
        而且报的是“找不到输出格式”，完全看不出和缩略图有关。
        """
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            # Simulate ffmpeg writing the file it was asked for.
            Path(command[-1]).write_bytes(b"jpeg")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        target = os.path.join(self.temp_root, "thumb.jpg")
        with patch.object(video_controller.subprocess, "run", side_effect=fake_run):
            built = video_controller._build_material_thumbnail(
                os.path.join(self.cache, "vid-a.mp4"), target
            )

        self.assertTrue(built)
        self.assertTrue(os.path.exists(target))
        command = commands[0]
        self.assertTrue(command[-1].endswith(".jpg"), "temp file must stay a .jpg")
        self.assertIn("-update", command, "image2 needs -update for a single frame")
        self.assertFalse(
            [f for f in os.listdir(self.temp_root) if ".part." in f],
            "no temporary file may be left behind",
        )

    def test_delete_removes_the_thumbnail_too(self):
        """缩略图来自已删除的文件，留着只会成为永远不会被清理的垃圾。"""
        thumbs = os.path.join(self.temp_root, "thumbnails")
        os.makedirs(thumbs)

        def storage_dir(sub_dir="", create=False):
            return thumbs if sub_dir == "thumbnails" else self.cache

        with patch.object(
            video_controller.utils, "storage_dir", side_effect=storage_dir
        ):
            # Written through the same helper the route uses: a test that
            # invents the file name would keep passing after the key changes,
            # while the real thumbnail was left on disk.
            thumbnail_path = video_controller._thumbnail_path_for(
                os.path.join(self.cache, "vid-a.mp4")
            )
            Path(thumbnail_path).write_bytes(b"jpeg")

            video_controller.delete_downloaded_material(
                self._request(), file_name="vid-a.mp4"
            )

        self.assertFalse(os.path.exists(thumbnail_path))

    def test_thumbnail_key_separates_files_that_share_a_name(self):
        """
        每个任务都会写出 final-1.mp4。按文件名做缓存键时，作品库里所有卡片
        都会显示第一个任务的画面——这正是这个用例要挡住的回归。
        """
        first = os.path.join(self.temp_root, "task-a", "final-1.mp4")
        second = os.path.join(self.temp_root, "task-b", "final-1.mp4")
        for path in (first, second):
            os.makedirs(os.path.dirname(path))
            Path(path).write_bytes(b"video")

        thumbs = os.path.join(self.temp_root, "thumbnails")
        os.makedirs(thumbs)
        with patch.object(
            video_controller.utils, "storage_dir", return_value=thumbs
        ):
            self.assertNotEqual(
                video_controller._thumbnail_path_for(first),
                video_controller._thumbnail_path_for(second),
            )

    def test_delete_rejects_a_path_outside_the_cache(self):
        """删除接口接收文件名，越界路径必须在触碰磁盘之前被拒绝。"""
        outside = os.path.join(self.temp_root, "outside.mp4")
        with patch.object(
            video_controller.utils, "storage_dir", return_value=self.cache
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.delete_downloaded_material(
                    self._request(), file_name=os.path.join("..", "outside.mp4")
                )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertTrue(os.path.exists(outside), "nothing outside may be deleted")


class TestLocalMaterialPreview(unittest.TestCase):
    """
    审阅界面要能播放用户自己上传的替换素材，同时不能越出上传目录。
    """

    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_serves_an_uploaded_clip_with_range_support(self):
        """播放器靠 Range 拖动进度条，预览路由必须和 /stream/ 行为一致。"""

        async def consume(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "mine.mp4").write_bytes(b"0123456789")
            with patch.object(
                video_controller.utils, "storage_dir", return_value=temp_dir
            ):
                response = asyncio.run(
                    video_controller.preview_local_material(
                        self._request("bytes=2-5"), "mine.mp4"
                    )
                )
                body = asyncio.run(consume(response))

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(body, b"2345")

    def test_rejects_traversal_out_of_the_uploads_directory(self):
        """
        预览路由同样接收客户端路径。没有目录约束，它就会变成任意文件读取。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            uploads = os.path.join(temp_dir, "local_videos")
            os.makedirs(uploads)
            Path(temp_dir, "secret.mp4").write_bytes(b"secret")
            with patch.object(
                video_controller.utils, "storage_dir", return_value=uploads
            ):
                with self.assertRaises(HttpException) as raised:
                    asyncio.run(
                        video_controller.preview_local_material(
                            self._request(), "../secret.mp4"
                        )
                    )

        self.assertEqual(raised.exception.status_code, 403)

    def test_stream_route_still_only_serves_the_task_directory(self):
        """
        新增预览路由不能顺带放宽 /stream/ 的约束，两条路由的根目录互不影响。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            tasks = os.path.join(temp_dir, "tasks")
            os.makedirs(tasks)
            Path(temp_dir, "outside.mp4").write_bytes(b"data")
            with patch.object(
                video_controller.utils, "task_dir", return_value=tasks
            ):
                with self.assertRaises(HttpException) as raised:
                    asyncio.run(
                        video_controller.stream_video(
                            self._request(), "../outside.mp4"
                        )
                    )

        self.assertEqual(raised.exception.status_code, 403)


class TestReviewedMaterialsRender(unittest.TestCase):
    """
    审阅后的素材路径由客户端回传，必须当作不可信输入校验。
    """

    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.task_dir = os.path.join(self.temp_root, "tasks", "task-123")
        self.uploads_dir = os.path.join(self.temp_root, "storage", "local_videos")
        os.makedirs(self.task_dir)
        os.makedirs(self.uploads_dir)

        self.task_clip = os.path.join(self.task_dir, "vid-1.mp4")
        self.uploaded_clip = os.path.join(self.uploads_dir, "mine.mp4")
        self.outside_clip = os.path.join(self.temp_root, "secret.mp4")
        for created in (self.task_clip, self.uploaded_clip, self.outside_clip):
            Path(created).write_bytes(b"data")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _patched_dirs(self):
        return (
            patch.object(
                video_controller.utils, "task_dir", return_value=self.task_dir
            ),
            patch.object(
                video_controller.utils, "storage_dir", return_value=self.uploads_dir
            ),
        )

    def test_accepts_downloaded_and_uploaded_clips(self):
        """任务目录里的下载素材和本地上传素材都是合法来源。"""
        task_patch, storage_patch = self._patched_dirs()
        with task_patch, storage_patch:
            resolved = video_controller._resolve_reviewed_materials(
                "task-123",
                [self.task_clip, self.uploaded_clip],
                "request-123",
            )

        self.assertEqual(
            [os.path.realpath(p) for p in resolved],
            [os.path.realpath(self.task_clip), os.path.realpath(self.uploaded_clip)],
        )

    def test_rejects_path_outside_allowed_directories(self):
        """
        目录外的路径必须被拒绝。否则调用方可以让任意本机文件被合成进视频，
        把渲染结果变成一条读取任意文件的通道。
        """
        task_patch, storage_patch = self._patched_dirs()
        with task_patch, storage_patch:
            with self.assertRaises(HttpException) as raised:
                video_controller._resolve_reviewed_materials(
                    "task-123", [self.outside_clip], "request-123"
                )

        self.assertEqual(raised.exception.status_code, 403)

    def test_rejects_traversal_escaping_the_task_directory(self):
        """`..` 逃逸同样必须被拒绝，不能只做字符串前缀判断。"""
        task_patch, storage_patch = self._patched_dirs()
        traversal = os.path.join(self.task_dir, "..", "..", "secret.mp4")
        with task_patch, storage_patch:
            with self.assertRaises(HttpException) as raised:
                video_controller._resolve_reviewed_materials(
                    "task-123", [traversal], "request-123"
                )

        self.assertEqual(raised.exception.status_code, 403)

    def test_render_requires_an_existing_task(self):
        body = SimpleNamespace(materials=["clip.mp4"])
        with patch.object(video_controller.sm.state, "get_task", return_value=None):
            with self.assertRaises(HttpException) as raised:
                video_controller.render_reviewed_task(
                    MagicMock(), self._request(), body, task_id="missing"
                )

        self.assertEqual(raised.exception.status_code, 404)

    def test_render_rejects_an_empty_approval(self):
        """全部素材都被移除时应给出 400，而不是排出一个必然失败的渲染。"""
        body = SimpleNamespace(materials=[])
        with patch.object(
            video_controller.sm.state, "get_task", return_value={"task_id": "task-123"}
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.render_reviewed_task(
                    MagicMock(), self._request(), body, task_id="task-123"
                )

        self.assertEqual(raised.exception.status_code, 400)

    def test_render_queues_only_the_render_stage(self):
        """渲染阶段不能再次触发完整流水线，否则会重复调用 LLM 与 TTS。"""
        body = SimpleNamespace(materials=[self.task_clip])
        task_patch, storage_patch = self._patched_dirs()

        with (
            task_patch,
            storage_patch,
            patch.object(
                video_controller.sm.state,
                "get_task",
                return_value={"task_id": "task-123"},
            ),
            patch.object(video_controller.task_manager, "add_task") as add_task,
        ):
            response = video_controller.render_reviewed_task(
                MagicMock(), self._request(), body, task_id="task-123"
            )

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"]["task_id"], "task-123")
        queued = add_task.call_args
        self.assertIs(queued.args[0], video_controller.tm.render_reviewed_materials)
        self.assertEqual(queued.kwargs["task_id"], "task-123")


class TestBackgroundMusicPreview(unittest.TestCase):
    """
    试听接口直接把文件内容发给浏览器，路径解析必须复用渲染那一套白名单，
    Content-Type 也必须是浏览器真的能解码的类型。
    """

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.songs = os.path.join(self.temp_root, "songs")
        self.uploads = os.path.join(self.temp_root, "bgm")
        os.makedirs(self.songs)
        os.makedirs(self.uploads)
        # A name with a space: the library ships several, and they have to
        # survive the round trip through the URL.
        self.track = Path(self.songs, "Nocturnal Pursuit.mp3")
        self.track.write_bytes(b"id3" + bytes(range(256)) * 8)
        Path(self.uploads, "mine.flac").write_bytes(b"fLaC" + b"x" * 400)
        Path(self.temp_root, "outside.mp3").write_bytes(b"nope")

        self.patches = [
            patch.object(
                video_controller.bgm_service, "uploaded_bgm_dir",
                return_value=self.uploads,
            ),
            patch.object(
                video_controller.bgm_service.utils, "song_dir",
                return_value=self.songs,
            ),
        ]
        for started in self.patches:
            started.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(shutil.rmtree, self.temp_root, True)
        self.client = TestClient(asgi.app)

    def _get(self, name, **kwargs):
        from urllib.parse import quote

        return self.client.get(f"/api/v1/music_preview/{quote(name)}", **kwargs)

    def test_serves_a_bundled_track_with_a_playable_content_type(self):
        response = self._get("Nocturnal Pursuit.mp3")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.content, self.track.read_bytes())

    def test_serves_an_uploaded_track_too(self):
        response = self._get("mine.flac")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-type"], "audio/flac")

    def test_a_range_request_returns_only_that_slice(self):
        """播放器拖动进度条依赖 Range；整段返回会让长曲目无法定位。"""
        response = self._get("Nocturnal Pursuit.mp3", headers={"Range": "bytes=10-19"})

        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.content), 10)
        self.assertEqual(response.content, self.track.read_bytes()[10:20])
        self.assertTrue(
            response.headers["content-range"].startswith("bytes 10-19/")
        )

    def test_paths_outside_the_music_directories_are_refused(self):
        for name in ("outside.mp3", os.path.join("..", "outside.mp3"), "missing.mp3"):
            with self.subTest(name=name):
                self.assertIn(self._get(name).status_code, (403, 404))

    def test_a_non_audio_name_is_refused(self):
        """扩展名校验来自 resolve_bgm_file，试听不能成为绕过它的旁路。"""
        Path(self.songs, "config.toml").write_bytes(b"secret = 1")
        self.assertEqual(self._get("config.toml").status_code, 403)

    def test_every_playable_upload_format_has_a_media_type(self):
        """
        <audio> 按 Content-Type 选解码器，落到 octet-stream 就是静默不可播。
        wma 故意不在表内：没有浏览器引擎能解码它，这里把这个取舍固定下来。
        """
        supported = set(video_controller.bgm_service.SUPPORTED_BGM_EXTENSIONS)
        mapped = set(video_controller._AUDIO_MEDIA_TYPES)

        self.assertEqual(supported - mapped, {".wma"})
        self.assertFalse(mapped - supported, "no type for a format uploads reject")


class TestReopenTaskForReviewHTTP(unittest.TestCase):
    """
    走真实路由的回归测试。上面的用例直接调用函数，所以装饰器绑到了哪个函数
    它们一概看不见——一个辅助函数被插到 @router.post 和端点之间时，测试全绿，
    而线上这个地址返回的是那个辅助函数的返回值。
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        config.app["api_key"] = ""
        self.client = TestClient(asgi.app)

        self.temp_root = tempfile.mkdtemp()
        self.tasks_root = os.path.join(self.temp_root, "tasks")
        self.task_dir = os.path.join(self.tasks_root, "task-123")
        os.makedirs(self.task_dir)
        self.clip = os.path.join(self.task_dir, "vid-1.mp4")
        Path(self.clip).write_bytes(b"video")
        Path(self.task_dir, "audio.mp3").write_bytes(b"audio")

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _task_dir(self, sub_dir=""):
        return os.path.join(self.tasks_root, sub_dir) if sub_dir else self.tasks_root

    def test_the_route_answers_with_the_reopen_payload(self):
        with (
            patch.object(
                video_controller.utils, "task_dir", side_effect=self._task_dir
            ),
            patch.object(
                video_controller.utils, "storage_dir", return_value=self.temp_root
            ),
            patch.object(
                video_controller.sm.state,
                "get_task",
                return_value={"task_id": "task-123", "script": "a script"},
            ),
        ):
            response = self.client.post(
                "/api/v1/tasks/task-123/reopen", json={"materials": [self.clip]}
            )

        self.assertEqual(response.status_code, 200)
        # The envelope, not a bare value: the client reads everything out of
        # `data`, so a route bound to the wrong function shows up right here.
        data = response.json()["data"]
        self.assertEqual(data["task_id"], "task-123")
        self.assertEqual(data["material_paths"], [self.clip])
        self.assertTrue(data["renderable"])
        self.assertFalse(data["needs_narration"])


class TestTaskResponseAudio(unittest.TestCase):
    """
    配音和成片一样是任务产物：前端要播放它，也要从它读出视频时长。
    响应里放绝对路径既播不了，也把宿主机目录暴露了出去。
    """

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.tasks_root = os.path.join(self.temp_root, "tasks")
        os.makedirs(os.path.join(self.tasks_root, "t1"))
        # The path is only rewritten for a file that is really there, the same
        # rule the render outputs go through.
        self.audio = os.path.join(self.tasks_root, "t1", "audio.mp3")
        Path(self.audio).write_bytes(b"audio")
        self.addCleanup(shutil.rmtree, self.temp_root, True)

    def _shape(self, task: dict) -> dict:
        with patch.object(
            video_controller.utils, "task_dir", return_value=self.tasks_root
        ):
            return video_controller._task_response_data(task, "request-1")

    def test_the_narration_is_returned_as_a_playable_path(self):
        data = self._shape(
            {
                "task_id": "t1",
                "audio_file": self.audio,
                "audio_duration": 42,
            }
        )

        self.assertEqual(data["audio_file"], "/tasks/t1/audio.mp3")
        self.assertNotIn(
            os.sep,
            data["audio_file"].replace("/", ""),
            "an absolute host path must never reach the client",
        )
        # The length rides along untouched: it is a number, not a path.
        self.assertEqual(data["audio_duration"], 42)

    def test_a_silent_task_has_no_narration_to_return(self):
        """无旁白渲染存的是空字符串，不该被包装成一个指向空路径的 URL。"""
        data = self._shape({"task_id": "t1", "audio_file": ""})

        self.assertEqual(data["audio_file"], "")


class TestStreamableMaterials(unittest.TestCase):
    """
    审阅界面靠这些 URI 播放素材。渲染是否经过审阅决定了片段落在任务目录还是
    共享缓存，两处都能播放；只认任务目录会让“不勾选审阅”的成片在编辑面板里
    全部显示 no preview。
    """

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.tasks_root = os.path.join(self.temp_root, "tasks")
        self.uploads = os.path.join(self.temp_root, "local_videos")
        self.cache = os.path.join(self.temp_root, "cache_videos")
        for directory in (self.tasks_root, self.uploads, self.cache):
            os.makedirs(directory)

        self.reviewed = os.path.join(self.tasks_root, "task-a", "vid-1.mp4")
        os.makedirs(os.path.dirname(self.reviewed))
        self.downloaded = os.path.join(self.cache, "vid-abc123.mp4")
        self.uploaded = os.path.join(self.uploads, "3f2b.mp4")
        for path in (self.reviewed, self.downloaded, self.uploaded):
            Path(path).write_bytes(b"video")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _storage_dir(self, sub_dir="", create=False):
        return {"local_videos": self.uploads, "cache_videos": self.cache}.get(
            sub_dir, self.temp_root
        )

    def _uris(self, paths, endpoint=""):
        with patch.object(
            video_controller.utils, "storage_dir", side_effect=self._storage_dir
        ):
            return video_controller._streamable_materials(
                paths, endpoint, self.tasks_root
            )

    def test_a_reviewed_clip_streams_from_the_task_directory(self):
        self.assertEqual(self._uris([self.reviewed]), ["/tasks/task-a/vid-1.mp4"])

    def test_a_cached_download_streams_from_the_material_route(self):
        """
        只有审阅流程会把下载强制写进任务目录。直接渲染的片段留在共享缓存里，
        照样有自己的播放路由。
        """
        self.assertEqual(
            self._uris([self.downloaded]),
            ["/api/v1/material_preview/vid-abc123.mp4"],
        )

    def test_an_uploaded_clip_streams_from_the_material_route(self):
        self.assertEqual(
            self._uris([self.uploaded]), ["/api/v1/material_preview/3f2b.mp4"]
        )

    def test_the_configured_endpoint_prefixes_both_routes(self):
        self.assertEqual(
            self._uris([self.reviewed, self.downloaded], endpoint="http://host:8080/"),
            [
                "http://host:8080/tasks/task-a/vid-1.mp4",
                "http://host:8080/api/v1/material_preview/vid-abc123.mp4",
            ],
        )

    def test_a_name_with_a_space_survives_the_material_route(self):
        """未转义的空格会把路径截断，播放器只会拿到 404。"""
        spaced = os.path.join(self.cache, "my clip.mp4")
        Path(spaced).write_bytes(b"video")

        self.assertEqual(
            self._uris([spaced]), ["/api/v1/material_preview/my%20clip.mp4"]
        )

    def test_anything_outside_every_root_has_no_preview(self):
        """
        目录之外的路径不能变成可取的 URL，哪怕它就在任务目录旁边。
        """
        outside = os.path.join(self.temp_root, "elsewhere.mp4")
        Path(outside).write_bytes(b"video")

        self.assertEqual(self._uris([outside]), [None])

    def test_a_deleted_clip_has_no_preview(self):
        """缓存可以被用户清空；缺失的文件要报 null，而不是一个坏播放器。"""
        os.remove(self.downloaded)

        self.assertEqual(self._uris([self.downloaded]), [None])

    def test_remote_footage_has_no_preview(self):
        """从未落盘的远端素材，webview 的内容策略也取不到。"""
        self.assertEqual(self._uris(["https://example.com/clip.mp4"]), [None])

    def test_the_result_stays_aligned_with_the_paths_given(self):
        """
        UI 用下标把 URI 和磁盘路径配对。少一个元素，整份列表就会错位，
        每个片段都指向别人的文件。
        """
        paths = [self.reviewed, "https://example.com/clip.mp4", self.downloaded]

        self.assertEqual(len(self._uris(paths)), len(paths))


class TestMaterialThumbnailCache(unittest.TestCase):
    """
    缩略图是磁盘缓存，键必须能区分不同文件、并在源文件被覆盖后失效。
    这两点都出过问题：作品库里所有卡片曾显示同一帧。
    """

    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.tasks_root = os.path.join(self.temp_root, "tasks")
        self.thumbs = os.path.join(self.temp_root, "thumbnails")
        self.uploads = os.path.join(self.temp_root, "local_videos")
        self.cache = os.path.join(self.temp_root, "cache_videos")
        for directory in (self.thumbs, self.uploads, self.cache):
            os.makedirs(directory)

        self.first = os.path.join(self.tasks_root, "task-a", "final-1.mp4")
        self.second = os.path.join(self.tasks_root, "task-b", "final-1.mp4")
        for path in (self.first, self.second):
            os.makedirs(os.path.dirname(path))
            Path(path).write_bytes(b"video")

        self.built = []

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _storage_dir(self, sub_dir="", create=False):
        return {
            "thumbnails": self.thumbs,
            "local_videos": self.uploads,
            "cache_videos": self.cache,
        }.get(sub_dir, self.temp_root)

    def _fake_build(self, source_path, thumbnail_path):
        self.built.append(source_path)
        # Content that identifies the source, so a mixed-up key is visible.
        Path(thumbnail_path).write_bytes(source_path.encode("utf-8"))
        return True

    def _patches(self):
        return (
            patch.object(
                video_controller.utils, "task_dir", return_value=self.tasks_root
            ),
            patch.object(
                video_controller.utils, "storage_dir", side_effect=self._storage_dir
            ),
            patch.object(
                video_controller,
                "_build_material_thumbnail",
                side_effect=self._fake_build,
            ),
        )

    def _get(self, key):
        task_patch, storage_patch, build_patch = self._patches()
        with task_patch, storage_patch, build_patch:
            return video_controller.get_material_thumbnail(self._request(), key)

    def test_tasks_sharing_an_output_name_get_their_own_frame(self):
        first = self._get("task-a/final-1.mp4")
        second = self._get("task-b/final-1.mp4")

        self.assertNotEqual(first.path, second.path)
        self.assertEqual(Path(first.path).read_bytes(), os.path.realpath(self.first).encode())
        self.assertEqual(
            Path(second.path).read_bytes(), os.path.realpath(self.second).encode()
        )

    def test_a_cached_thumbnail_is_reused(self):
        self._get("task-a/final-1.mp4")
        self._get("task-a/final-1.mp4")
        self.assertEqual(len(self.built), 1)

    def test_a_thumbnail_older_than_its_video_is_rebuilt(self):
        """
        重新编辑会在原路径上覆盖成片。路径相同意味着缓存键相同，只有比源文件
        更旧这一条件能发现内容已经变了。
        """
        response = self._get("task-a/final-1.mp4")
        thumbnail_path = response.path
        # Age the thumbnail past the video it came from.
        stale = os.path.getmtime(self.first) - 60
        os.utime(thumbnail_path, (stale, stale))

        self._get("task-a/final-1.mp4")
        self.assertEqual(len(self.built), 2)

    def test_the_response_is_not_cached_for_a_week(self):
        """
        成片重渲染后 URL 不变。长 max-age 会让浏览器整整一周显示旧封面。
        """
        response = self._get("task-a/final-1.mp4")
        self.assertNotIn("max-age=604800", response.headers.get("cache-control", ""))

    def test_deleting_a_task_takes_its_thumbnails_with_it(self):
        first = self._get("task-a/final-1.mp4").path
        second = self._get("task-b/final-1.mp4").path

        task_patch, storage_patch, _ = self._patches()
        with (
            task_patch,
            storage_patch,
            patch.object(
                video_controller.sm.state,
                "get_task",
                return_value={"task_id": "task-a", "state": const.TASK_STATE_COMPLETE},
            ),
            patch.object(video_controller.sm.state, "delete_task"),
        ):
            video_controller.delete_video(self._request(), task_id="task-a")

        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(second), "other tasks keep their thumbnails")


class TestReopenTaskForReview(unittest.TestCase):
    """
    重新编辑已保存项目时，片段列表由客户端回传，和渲染接口一样不可信。
    这里同时固定“文件被删掉不等于路径非法”这一区分：缓存目录用户可以随时
    清空，为此整单拒绝会让项目再也打不开。
    """

    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.tasks_root = os.path.join(self.temp_root, "tasks")
        self.task_dir = os.path.join(self.tasks_root, "task-123")
        self.uploads_dir = os.path.join(self.temp_root, "storage", "local_videos")
        os.makedirs(self.task_dir)
        os.makedirs(self.uploads_dir)

        self.first_clip = os.path.join(self.task_dir, "vid-1.mp4")
        self.second_clip = os.path.join(self.task_dir, "vid-2.mp4")
        self.audio = os.path.join(self.task_dir, "audio.mp3")
        for created in (self.first_clip, self.second_clip, self.audio):
            Path(created).write_bytes(b"data")
        self.deleted_clip = os.path.join(self.task_dir, "vid-gone.mp4")
        self.outside_clip = os.path.join(self.temp_root, "secret.mp4")
        Path(self.outside_clip).write_bytes(b"data")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _task_dir(self, sub_dir=""):
        return os.path.join(self.tasks_root, sub_dir) if sub_dir else self.tasks_root

    # Distinct from None, which is a meaningful value here: "the task is gone".
    _DEFAULT_TASK = object()

    def _reopen(self, materials, task=_DEFAULT_TASK):
        if task is self._DEFAULT_TASK:
            task = {"task_id": "task-123"}
        body = SimpleNamespace(materials=materials)
        with (
            patch.object(
                video_controller.utils, "task_dir", side_effect=self._task_dir
            ),
            patch.object(
                video_controller.utils, "storage_dir", return_value=self.uploads_dir
            ),
            patch.object(
                video_controller.sm.state,
                "get_task",
                return_value=task,
            ),
        ):
            return video_controller.reopen_task_for_review(
                self._request(), body, task_id="task-123"
            )

    def test_returns_saved_clips_in_the_order_they_were_saved(self):
        response = self._reopen([self.second_clip, self.first_clip])
        data = response["data"]

        self.assertEqual(
            [os.path.basename(path) for path in data["material_paths"]],
            ["vid-2.mp4", "vid-1.mp4"],
        )
        self.assertEqual(
            data["materials"],
            ["/tasks/task-123/vid-2.mp4", "/tasks/task-123/vid-1.mp4"],
        )
        self.assertEqual(data["missing"], 0)
        self.assertTrue(data["renderable"])

    def test_deleted_clips_are_dropped_and_counted(self):
        data = self._reopen([self.first_clip, self.deleted_clip])["data"]

        self.assertEqual(len(data["material_paths"]), 1)
        self.assertEqual(data["missing"], 1)
        self.assertTrue(data["renderable"])

    def test_a_clip_outside_the_allowed_directories_is_refused(self):
        with self.assertRaises(HttpException) as raised:
            self._reopen([self.outside_clip])
        self.assertEqual(raised.exception.status_code, 403)

    def test_missing_task_is_a_404(self):
        with self.assertRaises(HttpException) as raised:
            self._reopen([self.first_clip], task=None)
        self.assertEqual(raised.exception.status_code, 404)

    def test_a_silent_project_reopens_on_its_clips(self):
        """
        无旁白渲染本来就不写 audio.mp3。以它是否存在判断"能不能重渲染"，
        等于把这个功能生成的每个项目都判成素材丢失——而片段就在任务目录里。
        """
        os.remove(self.audio)

        data = self._reopen(
            [self.first_clip],
            task={"task_id": "task-123", "audio_file": "", "script": "a script"},
        )["data"]

        self.assertTrue(data["renderable"])
        self.assertEqual(len(data["material_paths"]), 1)
        # Nothing to re-record: this video never had a voice.
        self.assertFalse(data["needs_narration"])

    def test_a_restarted_session_reads_the_switch_off_the_manifest(self):
        """
        重启后任务记录由磁盘恢复，没有 audio_file 这一项。清单是渲染前写下
        的，参数都在里面，正好回答"这个视频到底有没有旁白"。
        """
        os.remove(self.audio)
        Path(self.task_dir, "script.json").write_text(
            json.dumps({"script": "a script", "params": {"voice_enabled": False}}),
            encoding="utf-8",
        )

        data = self._reopen([self.first_clip])["data"]

        self.assertTrue(data["renderable"])
        self.assertFalse(data["needs_narration"])

    def test_a_task_older_than_the_switch_counts_as_narrated(self):
        """清单里没有这个字段的旧任务本来就是有旁白的，缺了就得重录。"""
        os.remove(self.audio)
        Path(self.task_dir, "script.json").write_text(
            json.dumps({"script": "a script", "params": {"voice_name": "en-US"}}),
            encoding="utf-8",
        )

        data = self._reopen([self.first_clip])["data"]

        self.assertTrue(data["needs_narration"])

    def test_a_project_whose_clips_are_all_gone_is_not_renderable(self):
        """真正没有片段时仍然要说清楚：那是唯一无法继续的情况。"""
        data = self._reopen(
            [self.deleted_clip],
            task={"task_id": "task-123", "audio_file": "", "script": "a script"},
        )["data"]

        self.assertFalse(data["renderable"])
        self.assertEqual(data["material_paths"], [])
        self.assertEqual(data["missing"], 1)

    def test_a_missing_narration_is_re_recorded_rather_than_refused(self):
        """
        这条规则变了：审阅渲染不再要求旁白必须在磁盘上，缺了就重新录一次。
        继续判成不可渲染，只会把用户从一个完全可用的项目里赶出去——原来的
        用意是别让人点一个必然报错的按钮，而现在那个按钮不会报错了。
        """
        os.remove(self.audio)

        data = self._reopen([self.first_clip])["data"]

        self.assertTrue(data["renderable"])
        # 但值得提前说明：重录要花一次 TTS，而且用的是表单里当前的声音。
        self.assertTrue(data["needs_narration"])

    def test_empty_list_falls_back_to_the_task_record(self):
        """
        重启后按磁盘恢复的任务、以及记录片段之前保存的旧项目，都不带列表。
        素材仍在任务目录里，直接说“没有片段”是错的。
        """
        data = self._reopen(
            [], task={"task_id": "task-123", "materials": [self.second_clip]}
        )["data"]
        self.assertEqual(
            [os.path.basename(path) for path in data["material_paths"]], ["vid-2.mp4"]
        )

    def test_empty_list_falls_back_to_the_clips_in_the_task_directory(self):
        data = self._reopen([], task={"task_id": "task-123"})["data"]

        self.assertEqual(
            sorted(os.path.basename(path) for path in data["material_paths"]),
            ["vid-1.mp4", "vid-2.mp4"],
        )
        # 只挑源片段：成片和中间产物也在同一目录，混进来会被当成素材重新剪。
        self.assertNotIn(
            "audio.mp3", [os.path.basename(p) for p in data["material_paths"]]
        )


if __name__ == "__main__":
    unittest.main()
