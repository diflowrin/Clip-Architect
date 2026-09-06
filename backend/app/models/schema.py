import warnings
from enum import Enum
from typing import Any, List, Literal, Optional, Union

import pydantic
from pydantic import BaseModel, ConfigDict, Field

from app.config import config

# 忽略 Pydantic 的特定警告
warnings.filterwarnings(
    "ignore",
    category=UserWarning,
    message="Field name.*shadows an attribute in parent.*",
)


class VideoConcatMode(str, Enum):
    random = "random"
    sequential = "sequential"


class VideoTransitionMode(str, Enum):
    none = None
    shuffle = "Shuffle"
    fade_in = "FadeIn"
    fade_out = "FadeOut"
    slide_in = "SlideIn"
    slide_out = "SlideOut"
    zoom_in = "ZoomIn"
    zoom_out = "ZoomOut"


class VideoFillMode(str, Enum):
    """
    What fills the frame when a clip's shape does not match the video's.

    Stock footage is mostly landscape and most of what this app makes is
    vertical, so this decides what the majority of finished videos look like
    either side of the picture.
    """

    # A blurred, zoomed copy of the clip itself behind it. Nothing of the
    # source is lost and nothing is left empty, which is why it is the default.
    blur = "blur"
    # Fill the frame completely by cutting the sides off. Better when the
    # subject is centred, bad when it is not, and nothing can tell in advance.
    crop = "crop"
    # Black bars. What every render did before this setting existed.
    bars = "bars"


class VideoAspect(str, Enum):
    landscape = "16:9"
    portrait = "9:16"
    square = "1:1"

    def to_resolution(self):
        if self == VideoAspect.landscape:
            return 1920, 1080
        elif self == VideoAspect.portrait:
            return 1080, 1920
        elif self == VideoAspect.square:
            return 1080, 1080
        raise ValueError(f"unsupported video aspect: {self}")


_Config = ConfigDict(
    arbitrary_types_allowed=True,
    # Note: ensure your key names match renamed V2 parameters if needed
)


@pydantic.dataclasses.dataclass(config=_Config)
class MaterialInfo:
    provider: str = "pexels"
    url: str = ""
    duration: int = 0
    # 在线素材搜索会附带经过筛选的公开来源信息，供搜索缓存和任务记录复用。
    # 本地上传素材不需要填写；写入任务文件前仍会按字段白名单重新构造，
    # 避免外部请求传入的签名 URL、凭据或无关字段进入持久化数据。
    source_info: Optional[dict[str, Any]] = None


class VideoParams(BaseModel):
    """
    {
      "video_subject": "",
      "video_aspect": "横屏 16:9（西瓜视频）",
      "voice_name": "女生-晓晓",
      "bgm_name": "random",
      "font_name": "NotoSans-Bold.ttf",
      "text_color": "#FFFFFF",
      "font_size": 60,
      "stroke_color": "#000000",
      "stroke_width": 1.5
    }
    """

    video_subject: str
    video_script: str = ""  # Script used to generate the video
    video_terms: Optional[str | list] = None  # Keywords used to generate the video
    video_aspect: Optional[VideoAspect] = VideoAspect.portrait.value
    video_concat_mode: Optional[VideoConcatMode] = VideoConcatMode.random.value
    video_transition_mode: Optional[VideoTransitionMode] = None
    # How long one scene runs, in seconds. Zero - the default the app sends -
    # means varied: each scene draws 5, 10 or 15 seconds, which is what stops a
    # cut sounding like a metronome and roughly halves how many separate clips
    # a video downloads. A positive value pins every scene to that length.
    video_clip_duration: int = Field(default=0, ge=0)
    video_clip_speed: Optional[float] = 1.0
    # How a clip that is not the video's shape fills the frame. Defaults to a
    # blurred backdrop: black bars are the most visible sign of footage that
    # was never framed for the format it ended up in. An older project has no
    # value recorded and so re-renders with the blur too - the setting is on
    # the form for anyone who wants the bars back.
    video_fill_mode: Optional[VideoFillMode] = VideoFillMode.blur.value
    match_materials_to_script: bool = False
    video_count: int = Field(default=1, ge=1)
    # How long the finished video should run, in seconds.
    #
    # With no narration it is the length exactly: nothing else has an opinion.
    # With narration it is the length asked for - it becomes the word budget
    # the script is written to, and the voice track then decides the rest. It
    # is not a promise either way, which is why the cut is still timed against
    # the audio actually on disk. Not to be confused with
    # `video_clip_duration`, which is how long one scene runs.
    video_duration: int = Field(default=30, ge=1, le=600)

    video_source: Optional[str] = "pexels"
    video_materials: Optional[List[MaterialInfo]] = (
        None  # Materials used to generate the video
    )

    custom_audio_file: Optional[str] = (
        None  # Custom audio file path, will ignore TTS and can still use Whisper subtitles
    )
    video_language: Optional[str] = ""  # auto detect

    # Whether to record a narration at all. With it off the script is still
    # written - the search keywords come out of it - but nothing is spoken,
    # nothing is transcribed, and the video is footage plus whatever music is
    # configured. Subtitles go with it: their timings come from the TTS pass.
    voice_enabled: Optional[bool] = True
    voice_name: Optional[str] = ""
    voice_volume: Optional[float] = 1.0
    voice_rate: Optional[float] = 1.0
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = 0.2
    # Pull the music down while the narrator speaks and let it back up in the
    # gaps. Without it `bgm_volume` has to be set low enough to stay out of the
    # way of the voice, which leaves the music inaudible everywhere else.
    # Only ever applies when there is both a narration and music.
    bgm_ducking: Optional[bool] = True
    # 视频配乐供应商共用提示词，WebUI 新任务统一写入该字段。保留下面的
    # Sonilo 专用字段以兼容旧任务记录和现有 CLI 参数。
    video_music_prompt: str = Field(default="", max_length=2000)
    sonilo_bgm_prompt: str = Field(default="", max_length=2000)

    subtitle_enabled: Optional[bool] = True
    subtitle_position: Optional[str] = config.ui.get(
        "subtitle_position", "bottom"
    )  # top, bottom, center, custom
    custom_position: float = config.ui.get("custom_position", 70.0)
    font_name: Optional[str] = "NotoSans-Bold.ttf"
    text_fore_color: Optional[str] = "#FFFFFF"
    text_background_color: Union[bool, str] = False
    rounded_subtitle_background: bool = False

    font_size: int = 60
    stroke_color: Optional[str] = "#000000"
    stroke_width: float = 1.5
    n_threads: Optional[int] = 2
    paragraph_number: int = Field(default=1, ge=1, le=10)
    video_script_prompt: str = Field(default="", max_length=2000)
    custom_system_prompt: str = Field(default="", max_length=8000)


class SubtitleRequest(BaseModel):
    video_script: str
    video_language: Optional[str] = ""
    voice_name: Optional[str] = "zh-CN-XiaoxiaoNeural-Female"
    voice_volume: Optional[float] = 1.0
    voice_rate: Optional[float] = 1.2
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = 0.2
    subtitle_position: Optional[str] = config.ui.get("subtitle_position", "bottom")
    font_name: Optional[str] = "NotoSans-Bold.ttf"
    text_fore_color: Optional[str] = "#FFFFFF"
    text_background_color: Union[bool, str] = False
    rounded_subtitle_background: bool = False
    font_size: int = 60
    stroke_color: Optional[str] = "#000000"
    stroke_width: float = 1.5
    video_source: Optional[str] = "local"
    subtitle_enabled: Optional[str] = "true"


class AudioRequest(BaseModel):
    video_script: str
    video_language: Optional[str] = ""
    voice_name: Optional[str] = "zh-CN-XiaoxiaoNeural-Female"
    voice_volume: Optional[float] = 1.0
    voice_rate: Optional[float] = 1.2
    bgm_type: Optional[str] = "random"
    bgm_file: Optional[str] = ""
    bgm_volume: Optional[float] = 0.2
    video_source: Optional[str] = "local"


class VideoScriptParams:
    """
    {
      "video_subject": "春天的花海",
      "video_language": "",
      "paragraph_number": 1,
      "video_script_prompt": "",
      "custom_system_prompt": ""
    }
    """

    video_subject: Optional[str] = "春天的花海"
    video_language: Optional[str] = ""
    paragraph_number: int = Field(default=1, ge=1, le=10)
    # How long the script should take to read aloud. Zero keeps the old
    # behaviour for callers that never sent one: no budget, no trimming.
    video_duration: int = Field(default=0, ge=0, le=600)
    # What the finished video will look like, so the script can be written for
    # it. Optional, and empty means no hint at all rather than a guessed one:
    # an older client that never sent it keeps the script it used to get.
    video_aspect: Optional[str] = ""
    video_script_prompt: str = Field(default="", max_length=2000)
    custom_system_prompt: str = Field(default="", max_length=8000)


class VideoTermsParams:
    """
    {
      "video_subject": "",
      "video_script": "",
      "amount": 5,
      "match_materials_to_script": false
    }
    """

    video_subject: Optional[str] = "春天的花海"
    video_script: Optional[str] = (
        "春天的花海，如诗如画般展现在眼前。万物复苏的季节里，大地披上了一袭绚丽多彩的盛装。金黄的迎春、粉嫩的樱花、洁白的梨花、艳丽的郁金香……"
    )
    amount: Optional[int] = 5
    match_materials_to_script: bool = False


class VideoSocialMetadataParams:
    """
    {
      "video_subject": "A day in Shanghai",
      "video_script": "",
      "language": "auto",
      "platform": "tiktok"
    }
    """

    video_subject: Optional[str] = Field(default="A day in Shanghai", max_length=500)
    video_script: Optional[str] = Field(default="", max_length=8000)
    language: Optional[str] = Field(default="auto", max_length=64)
    platform: Optional[str] = Field(default="tiktok", max_length=64)


class TaskVideoRequest(VideoParams, BaseModel):
    pass


class RenderReviewedRequest(VideoParams, BaseModel):
    """
    渲染用户已确认的素材列表。

    ``materials`` 是审阅后保留的片段路径，顺序即时间线顺序。控制器会逐个
    校验它们仍位于任务目录或本地上传目录内，绝不能直接当作可信路径使用。
    """

    materials: List[str] = []


class TaskQueryRequest(BaseModel):
    pass


class ProjectSaveRequest(BaseModel):
    """
    保存一份可再次编辑的生成设置。

    ``params`` 保持原始字典：服务层会用 VideoParams 校验并归一化后再落盘，
    这样存下来的内容必定能直接回传给 /videos，而不是一份只能人工修补的
    半结构化数据。``task_id`` 只是来源记录，删除任务不会影响项目。
    """

    title: str = ""
    params: dict = Field(default_factory=dict)
    task_id: Optional[str] = None
    # 本次成片实际使用的片段路径，按时间线顺序。重新编辑时据此复用原素材，
    # 而不是重新匹配一批新片段。回传时一律重新校验，不能当作可信路径。
    materials: Optional[List[str]] = None


class ProjectUpdateRequest(BaseModel):
    """重命名或覆盖已保存项目；每个字段都可单独提交，省略即保持原值。"""

    title: Optional[str] = None
    params: Optional[dict] = None
    task_id: Optional[str] = None
    materials: Optional[List[str]] = None


class ReopenTaskRequest(BaseModel):
    """
    重新打开一次已完成任务的素材列表，用于再次审阅。

    ``materials`` 来自客户端保存的项目，因此和渲染接口一样，必须逐个校验
    仍位于任务目录或本地素材目录内。
    """

    materials: List[str] = []


class MaterialSearchRequest(BaseModel):
    """
    在审阅界面用一个关键词换掉某一段素材。

    ``exclude`` 是时间线上已有的片段路径，用于跳过重复候选；它只参与文件名
    比较，不会被当作可读取的路径使用。``video_aspect`` 与 ``video_clip_duration``
    保持和生成表单一致，否则换来的片段方向或时长与成片不匹配。
    """

    search_term: str = ""
    video_source: Optional[str] = "pexels"
    video_aspect: Optional[VideoAspect] = VideoAspect.portrait.value
    video_clip_duration: int = Field(default=0, ge=0)
    exclude: List[str] = []


class VideoScriptRequest(VideoScriptParams, BaseModel):
    pass


class VideoTermsRequest(VideoTermsParams, BaseModel):
    pass


class VideoSocialMetadataRequest(VideoSocialMetadataParams, BaseModel):
    pass


# ---------------------------
# ----- RESPONSE MODELS -----
# ---------------------------
class BaseResponse(BaseModel):
    status: int = 200
    message: Optional[str] = "success"
    data: Any = None


# ---- DATA MODELS ----
class TaskResponseData(BaseModel):
    task_id: str


class TaskStatusData(BaseModel):
    """任务查询对外保证的稳定字段；历史和扩展字段继续原样透传。"""

    model_config = ConfigDict(extra="allow")

    task_id: str
    state: int
    progress: int = 0
    videos: Optional[List[str]] = None
    combined_videos: Optional[List[str]] = None
    failed_stage: Optional[str] = None
    error: Optional[str] = None
    cross_post_state: Optional[
        Literal["pending", "processing", "complete", "failed"]
    ] = None
    cross_post_results: Optional[List[dict[str, Any]]] = None
    cross_post_error: Optional[str] = None


class TaskListData(BaseModel):
    """分页任务列表结构。"""

    tasks: List[TaskStatusData]
    total: int
    page: int
    page_size: int


class VideoScriptData(BaseModel):
    video_script: str


class VideoTermsData(BaseModel):
    video_terms: List[str]


class VideoSocialMetadataData(BaseModel):
    title: str
    caption: str
    hashtags: List[str]


class FileData(BaseModel):
    name: str
    size: int
    file: str


class DownloadedMaterialData(BaseModel):
    """
    已下载素材条目。比 FileData 多一个修改时间：素材缓存会长到上 GB，
    用户需要按“最近用过”判断哪些可以删除。
    """

    name: str
    size: int
    file: str
    modified: float


class DownloadedMaterialListData(BaseModel):
    files: List[DownloadedMaterialData]
    total_size: int


class BgmFileData(FileData):
    """
    一条背景音乐条目。

    ``bundled`` 表示该曲目随应用发布。只有这些曲目参与 "random" 抽取，
    用户上传的曲目仍可按名字选择，因此表单需要这个标记才能说清
    "每次渲染随机挑一首" 到底是从几首里挑。
    """

    bundled: bool = False


class BgmRetrieveData(BaseModel):
    files: List[BgmFileData]


class BgmUploadData(BaseModel):
    file: str


class VideoMaterialRetrieveData(BaseModel):
    files: List[FileData]


class VideoMaterialUploadData(BaseModel):
    file: str


class DownloadedMaterialRetrieveResponse(BaseResponse):
    data: DownloadedMaterialListData


# ---- RESPONSE MODELS ----
class TaskResponse(BaseResponse):
    data: TaskResponseData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                },
            },
        }
    )


class TaskQueryResponse(BaseResponse):
    """
    任务查询会返回生成状态和可选的跨平台发布状态。

    生成失败时包含 `failed_stage` 和 `error`；生成完成后如果启用了自动发布，
    `cross_post_state` 会依次进入 pending、processing、complete 或 failed。
    """

    data: TaskStatusData

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": 200,
                    "message": "success",
                    "data": {
                        "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                        "state": 1,
                        "progress": 100,
                        "videos": ["/tasks/example/final-1.mp4"],
                        "cross_post_state": "complete",
                        "cross_post_results": [{"success": True}],
                    },
                },
                {
                    "status": 200,
                    "message": "success",
                    "data": {
                        "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                        "state": -1,
                        "progress": 30,
                        "failed_stage": "audio",
                        "error": "TTS request timed out",
                    },
                },
            ],
        }
    )


class TaskListResponse(BaseResponse):
    """任务列表使用独立响应模型，避免与单任务查询混用文档结构。"""

    data: TaskListData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "tasks": [
                        {
                            "task_id": "6c85c8cc-a77a-42b9-bc30-947815aa0558",
                            "state": 4,
                            "progress": 50,
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 10,
                },
            }
        }
    )


class TaskDeletionResponse(BaseResponse):
    data: None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": None,
            },
        }
    )


class VideoScriptResponse(BaseResponse):
    data: VideoScriptData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "video_script": "春天的花海，是大自然的一幅美丽画卷。在这个季节里，大地复苏，万物生长，花朵争相绽放，形成了一片五彩斑斓的花海..."
                },
            },
        }
    )


class VideoTermsResponse(BaseResponse):
    data: VideoTermsData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {"video_terms": ["sky", "tree"]},
            },
        }
    )


class VideoSocialMetadataResponse(BaseResponse):
    data: VideoSocialMetadataData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "title": "A Day in Shanghai You Should Not Miss",
                    "caption": "Save this quick Shanghai inspiration and follow for more short travel ideas.",
                    "hashtags": ["#shorts", "#travel", "#shanghai", "#viral", "#fyp"],
                },
            },
        }
    )


class BgmRetrieveResponse(BaseResponse):
    data: BgmRetrieveData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "files": [
                        {
                            "name": "4fca18fce7344f3aa824777a40d45c8c.mp3",
                            "size": 1891269,
                            "file": "4fca18fce7344f3aa824777a40d45c8c.mp3",
                            "bundled": True,
                        }
                    ]
                },
            },
        }
    )


class BgmUploadResponse(BaseResponse):
    data: BgmUploadData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {"file": "4fca18fce7344f3aa824777a40d45c8c.mp3"},
            },
        }
    )


class VideoMaterialRetrieveResponse(BaseResponse):
    data: VideoMaterialRetrieveData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "files": [
                        {
                            "name": "example.mp4",
                            "size": 12345678,
                            "file": "/MoneyPrinterTurbo/resource/videos/example.mp4",
                        }
                    ]
                },
            },
        }
    )


class VideoMaterialUploadResponse(BaseResponse):
    data: VideoMaterialUploadData

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": 200,
                "message": "success",
                "data": {
                    "file": "/MoneyPrinterTurbo/resource/videos/example.mp4",
                },
            },
        }
    )
