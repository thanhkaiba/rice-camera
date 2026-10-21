"""Load and validate config.yaml."""
from __future__ import annotations

import dataclasses
import datetime as dt
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


def parse_clock(value) -> dt.time:
    """Parse "HH:MM". YAML 1.1 reads an unquoted 10:00 as the base-60 integer 600, so accept that too."""
    if isinstance(value, int):
        return dt.time(value // 60, value % 60)
    return dt.datetime.strptime(str(value).strip(), "%H:%M").time()


LATE_MESSAGE_FIELDS = ("mention", "name", "time", "minutes")


def check_late_message_item(item: str) -> None:
    """Allow only the documented placeholders: a format string can otherwise reach into objects
    ({mention.__class__}) or crash on an unknown attribute."""
    error = ValueError("the late message line may only use {mention}, {name}, {time} and {minutes}")
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(item) if name is not None]
        item.format(mention="@A", name="A", time="10:15", minutes=15)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        raise error from None
    if any(name not in LATE_MESSAGE_FIELDS for name in fields):
        raise error


@dataclass
class ScheduleConfig:
    window_start: str = "08:00"  # camera starts processing
    late_after: str = "10:00"  # this whole minute is on time; late starts at 10:01
    window_end: str = "11:00"  # camera stops processing, daily summary is sent
    work_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Monday = 0

    def clocks(self) -> tuple[dt.time, dt.time, dt.time]:
        return parse_clock(self.window_start), parse_clock(self.late_after), parse_clock(self.window_end)


@dataclass
class CameraConfig:
    url: str = ""  # rtsp://..., a video file, or a webcam index such as "0"
    process_fps: float = 5.0
    # FFmpeg threads decoding the stream; 0 = FFmpeg's choice (all cores). A 3K H.265 stream on a
    # 12-thread laptop took ~80 % CPU with all cores and ~40 % with 2, at the same frame rate.
    decode_threads: int = 2
    enhance_low_light: bool = True
    # Part of the picture to analyse, as fractions "left,top,right,bottom" (e.g. "0.3,0.1,0.8,1");
    # empty = the whole picture. Cropping to the walkway makes faces bigger for the detector and
    # ignores people sitting elsewhere in the room. Easiest to set on the Live camera page.
    area: str = ""

    def area_box(self, shape) -> tuple[int, int, int, int] | None:
        """The area in pixels of a frame of `shape`, or None for the whole frame."""
        a = parse_area(self.area)
        if a is None:
            return None
        h, w = shape[:2]
        return int(a[0] * w), int(a[1] * h), int(a[2] * w), int(a[3] * h)


def parse_area(text: str) -> tuple[float, float, float, float] | None:
    """"left,top,right,bottom" fractions -> tuple, or None when empty. Raises ValueError."""
    if not text or not text.strip():
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in text.split(","))
    except ValueError:
        raise ValueError("camera.area must be four numbers: left,top,right,bottom (fractions 0-1)") from None
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError("camera.area must satisfy 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
    if x2 - x1 < 0.1 or y2 - y1 < 0.1:
        raise ValueError("camera.area is too small (at least a tenth of the picture each way)")
    return x1, y1, x2, y2


DEFAULT_MODELS = {
    "insightface": ("models/det_10g.onnx", "models/w600k_r50.onnx"),
    "opencv": ("models/face_detection_yunet_2023mar.onnx", "models/face_recognition_sface_2021dec.onnx"),
}


@dataclass
class RecognitionConfig:
    backend: str = "insightface"  # or "opencv"; see recognizer.py for the accuracy/licence trade-off
    detector_model: str = ""  # empty: the backend's default file under models/
    recognizer_model: str = ""
    det_size: int = 640
    use_gpu: bool = False
    # Threads for the models (and OpenCV). 0 = one per physical core, at most 6; the rest is left
    # for the video decoder and the admin page, so frames are not dropped while one is analysed.
    cpu_threads: int = 0
    # Quality gate: frames failing it never vote, they can only end up as "unknown".
    min_face_px: int = 60
    # Detector confidence for a frame to vote for a person. Faces from the detector's own cut-off (0.5,
    # or min_det_score if lower) are followed anyway and sent for review when nobody is recognised:
    # masks and side views score 0.5-0.65, and they make faces of different people look alike.
    min_det_score: float = 0.65
    min_sharpness: float = 40.0
    max_yaw: float = 35.0
    max_pitch: float = 30.0
    # Decision: tune accept_score and min_margin with `python -m attendance evaluate`.
    accept_score: float = 0.45
    # Remove the direction all face embeddings share before comparing (see gallery.py). Scores
    # get lower, so set accept_score again from `evaluate`, which compares both ways.
    center_embeddings: bool = False
    # Judge head pose relative to how this camera normally sees faces (learned from clear matches),
    # so a camera on a desk looking up, or above a door looking down, does not reject every face.
    auto_pose: bool = True
    min_margin: float = 0.08
    votes_required: int = 3
    track_timeout_s: float = 1.5
    unknown_min_frames: int = 3
    # Review and learning. Faces named on the review page are always learned; these tune the rest.
    learn_corrections: bool = True  # also learn a face the admin moves to the right person on the Today page
    learn_per_group: int = 5  # reference photos taken from one confirmed group of unknown faces
    group_days: int = 14  # how far back to compare photos of faces ignored today
    # An unclear face at least this similar to someone already checked in today is taken to be that
    # person passing again at a bad angle, and is not sent for review.
    seen_again_score: float = 0.3
    suggest_score: float = 0.35  # Review lists people from this similarity up as "Maybe" choices

    def model_files(self) -> tuple[str, str]:
        default_det, default_rec = DEFAULT_MODELS.get(self.backend, ("", ""))
        return self.detector_model or default_det, self.recognizer_model or default_rec

    def model_id(self) -> str:
        """Embeddings from different recognition models are not comparable, so they are stored per model."""
        return Path(self.model_files()[1]).stem


@dataclass
class TrackingConfig:
    """How faces are followed between frames.
    face: follow faces only (light). person: also detect and follow whole people, which keeps
    identities apart when several people walk in together (heavier; see docs/recognition.md)."""
    mode: str = "face"
    person_model: str = "models/yolox_tiny.onnx"  # YOLOX (Apache-2.0) or a YOLOv8 ONNX export (AGPL-3.0)
    person_conf: float = 0.5  # confidence needed to start following a person
    head_crop_size: int = 320  # faces are also searched in each person's head region at this size
    # The whole-frame face search is the slowest step. In person mode the head regions are
    # searched on every frame, the whole frame only every Nth frame (and whenever nobody is
    # followed) to catch faces whose body was missed. 1 = every frame.
    full_frame_every: int = 4


@dataclass
class GoogleChatConfig:
    webhook_url: str = ""
    mention_people: bool = True  # @mention late and missing people (needs their Google Chat user ID)
    notify_late_arrivals: bool = True
    unknown_batch_minutes: int = 5
    # How people are called in messages: "name" (as on the People page) or "email" (the part
    # before @, e.g. ruby@… → Ruby), for teams that know each other by their email names.
    names: str = "email"


@dataclass
class LateMessageConfig:
    """One Google Chat message a day listing who came late, with an @mention for each person."""
    enabled: bool = True
    intro: str = "Morning, coming late today:"
    item: str = "{mention} ({time})"  # placeholders: {mention}, {name}, {time}, {minutes}
    auto_send: bool = True
    send_at: str = ""  # empty: when the camera stops (window_end); can also be sent from the Today page
    # Attach one picture of the late people (the whole person as the camera saw them, name and time
    # burned in). Google Chat shows it only when web.public_url is an https address it can reach;
    # otherwise the message links to it.
    photo: bool = True


@dataclass
class LeaveApiConfig:
    """HR system API that provides holidays, extra work days and employee leave (see leave_api.py)."""
    url: str = ""  # empty: no leave data, only work_days decide who is expected
    token: str = ""  # sent as "Authorization: Bearer <token>"
    refresh_minutes: int = 15
    days_back: int = 7  # range fetched around today
    days_ahead: int = 60


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = "http://localhost:8000"  # link put into Google Chat messages
    username: str = "admin"
    password: str = ""  # empty disables login
    viewer_username: str = "viewer"  # a second, read-only login (Today, Review, Reports); needs `password` set
    viewer_password: str = ""  # empty: no read-only login


@dataclass
class UpdatesConfig:
    # Packaged program only (on by default there): install new releases by itself, outside camera hours.
    enabled: bool = field(default_factory=lambda: getattr(sys, "frozen", False))
    check_hours: float = 6


@dataclass
class BackupConfig:
    """Daily copy of the database, settings and photos to another place (see backup.py)."""
    folder: str = ""  # empty = no backups. An external drive, a network share, or a cloud-synced folder
    time: str = "12:00"  # after camera hours, when the computer has nothing else to do
    keep_days: int = 30  # daily database copies kept
    include_photos: bool = True  # camera pictures (crops/) too, not only the reference photos


@dataclass
class Config:
    timezone: str = "Asia/Ho_Chi_Minh"
    data_dir: str = "data"
    employees_file: str = "employees.csv"  # email,name CSV, imported initially or once to attach legacy emails
    healthcheck_url: str = ""
    retention_days: int = 90  # face photos of arrivals and unknown faces are deleted after this
    # Snapshots of a day on which someone was late are kept this long instead, as evidence in case
    # the person disputes it (never shorter than retention_days).
    late_evidence_days: int = 365
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    google_chat: GoogleChatConfig = field(default_factory=GoogleChatConfig)
    leave_api: LeaveApiConfig = field(default_factory=LeaveApiConfig)
    late_message: LateMessageConfig = field(default_factory=LateMessageConfig)
    web: WebConfig = field(default_factory=WebConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    base_dir: Path = field(default=Path("."), repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.base_dir / path

    def validate(self) -> None:
        start, late, end = self.schedule.clocks()
        # Store times as "HH:MM" even if YAML read them as numbers.
        self.schedule.window_start, self.schedule.late_after, self.schedule.window_end = (
            t.strftime("%H:%M") for t in (start, late, end))
        if not start < late <= end:
            raise ValueError("schedule must satisfy window_start < late_after <= window_end")
        if self.recognition.backend not in DEFAULT_MODELS:
            raise ValueError(f"recognition.backend must be one of {sorted(DEFAULT_MODELS)}")
        if any(d not in range(7) for d in self.schedule.work_days):
            raise ValueError("work_days must contain numbers 0 (Monday) to 6 (Sunday)")
        r = self.recognition
        if not (0 < r.accept_score < 1 and 0 <= r.min_margin < 1 and 0 <= r.min_det_score <= 1):
            raise ValueError("accept_score, min_margin and min_det_score must be between 0 and 1")
        if r.votes_required < 1 or r.unknown_min_frames < 1 or r.min_face_px < 1:
            raise ValueError("votes_required, unknown_min_frames and min_face_px must be at least 1")
        if r.learn_per_group < 1 or r.group_days < 1:
            raise ValueError("learn_per_group and group_days must be at least 1")
        if not 0 <= r.seen_again_score < 1:
            raise ValueError("seen_again_score must be between 0 and 1")
        if not 0 < r.suggest_score < 1:
            raise ValueError("suggest_score must be between 0 and 1")
        if not 0 < self.camera.process_fps <= 30:
            raise ValueError("process_fps must be between 0 and 30")
        if not 0 <= self.camera.decode_threads <= 32:
            raise ValueError("camera.decode_threads must be between 0 (FFmpeg's choice) and 32")
        if r.det_size < 160 or (r.backend == "insightface" and r.det_size % 32):
            raise ValueError("det_size must be at least 160 and, for insightface, a multiple of 32 (e.g. 640, 960)")
        if self.retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if self.late_evidence_days < 1:
            raise ValueError("late_evidence_days must be at least 1")
        self.web.viewer_username = self.web.viewer_username.strip()
        if self.web.viewer_password and (not self.web.viewer_username or self.web.viewer_username == self.web.username):
            raise ValueError("the read-only login needs its own user name, different from the admin's")
        self.backup.folder = self.backup.folder.strip()
        self.backup.time = parse_clock(self.backup.time).strftime("%H:%M")
        if self.backup.keep_days < 1:
            raise ValueError("backup.keep_days must be at least 1")
        lm = self.late_message
        if str(lm.send_at).strip():
            lm.send_at = parse_clock(lm.send_at).strftime("%H:%M")
            # At the late time itself nobody has come late yet, so the message would be empty.
            if parse_clock(lm.send_at) <= late:
                raise ValueError("the late message must be sent after the late time")
        else:
            lm.send_at = ""
        check_late_message_item(lm.item)
        if not 0 <= self.recognition.cpu_threads <= 64:
            raise ValueError("recognition.cpu_threads must be between 0 (automatic) and 64")
        a = parse_area(self.camera.area)
        self.camera.area = ",".join(f"{v:.3f}".rstrip("0").rstrip(".") or "0" for v in a) if a else ""
        if self.google_chat.names not in ("name", "email"):
            raise ValueError("google_chat.names must be name or email")
        t = self.tracking
        if t.mode not in ("face", "person"):
            raise ValueError("tracking.mode must be face or person")
        if not 0.1 <= t.person_conf <= 0.95:
            raise ValueError("tracking.person_conf must be between 0.1 and 0.95")
        if t.head_crop_size < 64 or t.head_crop_size % 32:
            raise ValueError("tracking.head_crop_size must be a multiple of 32, at least 64")
        if not 1 <= t.full_frame_every <= 30:
            raise ValueError("tracking.full_frame_every must be between 1 and 30")
        if self.leave_api.refresh_minutes < 1:
            raise ValueError("leave_api.refresh_minutes must be at least 1")
        if self.google_chat.unknown_batch_minutes < 0:
            raise ValueError("unknown_batch_minutes must not be negative")
        self.tz  # raises on an unknown time zone


def _build(cls, data: dict):
    if not isinstance(data, dict):
        raise ValueError(f"expected a mapping for {cls.__name__}")
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys for {cls.__name__}: {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        sub = known[name].default_factory if known[name].default_factory is not dataclasses.MISSING else None
        if sub is not None and dataclasses.is_dataclass(sub):
            kwargs[name] = _build(sub, value or {})
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load(path: str | Path) -> Config:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cfg = _build(Config, data)
    cfg.base_dir = path.resolve().parent
    cfg.validate()
    return cfg
