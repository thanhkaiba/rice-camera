"""Load and validate config.yaml."""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml


def parse_clock(value) -> dt.time:
    """Parse "HH:MM". YAML 1.1 reads an unquoted 10:00 as the base-60 integer 600, so accept that too."""
    if isinstance(value, int):
        return dt.time(value // 60, value % 60)
    return dt.datetime.strptime(str(value).strip(), "%H:%M").time()


LATE_MESSAGE_FIELDS = ("mention", "name", "time", "minutes", "registered")


def check_late_message_item(item: str) -> None:
    """Allow only the documented placeholders: a format string can otherwise reach into objects
    ({mention.__class__}) or crash on an unknown attribute."""
    error = ValueError("the late message line may only use {mention}, {name}, {time}, {minutes} and {registered}")
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(item) if name is not None]
        item.format(mention="@A", name="A", time="10:15", minutes=15, registered="")
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
    # Maintenance mode, switched on and off by the button on the portal (never by the Settings form): the
    # camera stream is not read, nobody is recognised or greeted, and a work day it touches inside the camera
    # hours is not counted (no late, not seen, fine or summary message for it).
    maintenance: bool = False
    maintenance_until: str = ""  # "YYYY-MM-DDTHH:MM" local time it ends by itself; empty: until switched off

    def clocks(self) -> tuple[dt.time, dt.time, dt.time]:
        return parse_clock(self.window_start), parse_clock(self.late_after), parse_clock(self.window_end)

    def maintenance_end(self, tz) -> dt.datetime | None:
        """When maintenance mode ends by itself, or None when it lasts until switched off."""
        return dt.datetime.fromisoformat(self.maintenance_until).replace(tzinfo=tz) if self.maintenance_until else None

    def in_maintenance(self, now: dt.datetime) -> bool:
        """Maintenance mode is on and its end time, if it has one, is still to come."""
        end = self.maintenance_end(now.tzinfo) if self.maintenance else None
        return self.maintenance and (end is None or now < end)


MAX_CAMERAS = 4


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
    # More cameras (a second door), one address per line. They share these settings, except the
    # detection area, which applies to the first camera only. Each camera costs as much CPU as the first.
    more: str = ""

    def urls(self) -> list[str]:
        """Every camera address: `url` first, then the lines of `more` (blank lines ignored)."""
        out = [self.url.strip()] if self.url.strip() else []
        return out + [line.strip() for line in self.more.splitlines() if line.strip()]

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
    # A smaller face (down to this size) still votes when it clearly matches one person (see
    # tracker.clear_match): at the door faces are often 40-60 px before the person comes closer.
    small_face_px: int = 40
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
    # person passing again at a bad angle, and is not sent for review. Never applied below
    # accept_score: a weaker resemblance is not them (0 = always review).
    seen_again_score: float = 0.45
    suggest_score: float = 0.35  # Review lists people from this similarity up as "Maybe" choices
    # After a face is confirmed on Review, other pending cards from the same day whose faces are at
    # least this similar to it are offered as the same person ("3 more cards look like X"), so one
    # person seen many times is named once. Compared face to face, not against the gallery. 0 = off.
    same_face_score: float = 0.55
    # Review lists camera faces below this Image score (0-100) apart, after the others, as blurry or
    # too small: most are unusable, but some are still someone arriving, so they are never dropped.
    blurry_score: int = 10
    # Housekeeping at midnight, where this program is in charge (the server, or a computer on its own):
    # Review cards of earlier days whose photos are all blurry (see blurry_score) are removed, and
    # reference photos with an Image score below this are deleted (0 = keep all). Never someone's
    # last MIN_REFERENCES photos: a poor photo recognises better than none.
    auto_delete_blurry: bool = True
    auto_delete_photos_below: int = 25

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
    # A second space for system news only (power unplugged, camera down, Base errors); empty: the webhook above.
    system_webhook_url: str = ""
    mention_people: bool = True  # @mention late and missing people (needs their Google Chat user ID)
    # Off by default, the space only gets the late message (late_message) and the problems: each of
    # these adds a message a day, or one per late person.
    notify_late_arrivals: bool = False  # each late arrival the camera sees, as it happens
    late_time_list: bool = False  # at the late time, who is not seen yet
    daily_summary: bool = False  # when the camera stops, the day's totals (on time, late, not seen, on leave)
    unknown_batch_minutes: int = 5
    # How people are called in messages: "name" (as on the People page) or "email" (the part
    # before @, e.g. ruby@… → Ruby), for teams that know each other by their email names.
    names: str = "email"


@dataclass
class PendingReminderConfig:
    """One Google Chat message a day reminding the approvers of the Base requests still pending."""
    enabled: bool = True
    auto_send: bool = True  # off: only the button on the Today page sends it
    time: str = "09:30"
    kinds: str = "1977"  # Base request types (group ids, comma separated) that are reminded; 1977 = Coming late


@dataclass
class LateMessageConfig:
    """One daily Google Chat message mentioning late arrivals and people not seen."""
    enabled: bool = True
    include_late: bool = True
    include_not_seen: bool = True
    intro: str = "Morning, coming late today:"
    # Placeholders: {mention}, {name}, {time}, {minutes}, {registered} (", registered 9:00-09:20" when
    # the person had a "coming late" permission on Base and still came after it, ", request pending 9:00-09:20"
    # when that request is not approved yet, else empty).
    item: str = "{mention} ({time}{registered})"
    auto_send: bool = True
    send_at: str = ""  # empty: when the camera stops (window_end); can also be sent from the Today page
    # Attach one picture of the late people (the whole person as the camera saw them, name and time
    # burned in). Google Chat shows it only when web.public_url is an https address it can reach;
    # otherwise the message links to it.
    photo: bool = True
    # Around holidays (Tet, Mid-Autumn, Christmas, Halloween, ...) the picture takes that holiday's background.
    occasions: bool = True
    # The one picture behind the cards, chosen on the Settings page (see latephoto.backgrounds);
    # empty: automatic, a different one each day and holiday pictures around holidays.
    background: str = ""
    # The Google Chat space the message is posted in through the Chat API (the ID, e.g. AAAAabc123, or
    # spaces/AAAAabc123), when it is not the notifications space. The signed-in account must be a member.
    # Empty, or when the Chat API is not signed in: the notifications space.
    space: str = ""

    def space_name(self) -> str:
        """"spaces/ID" for the Chat API, or "" for the notifications space."""
        return f"spaces/{self.space}" if self.space else ""


@dataclass
class FineNoticeConfig:
    """The morning's "cold fine" notice: one Google Chat message at `send_at` with the picture of the
    late people and a link to a public page (see core/fines.py) that lists who is fined, where they
    can appeal until `appeal_until`; approvers decide on their own page until `review_until`.
    When on, it takes the place of the automatic late message."""
    enabled: bool = False
    send_at: str = "09:50"
    list_until: str = "10:00"  # arrivals up to here move people on the list; later it stays as it is
    appeal_until: str = "10:10"  # the public page closes
    review_until: str = "10:14"  # the approvers' page closes; an appeal nobody decided stays a fine
    approvers: str = ""  # employee ids, comma separated: who may decide (they pick their name on the page)
    # Webhook of the Google Chat space of the approvers: each appeal is announced there with the review link.
    approver_webhook_url: str = field(default="", repr=False)

    def clocks(self) -> tuple[dt.time, dt.time, dt.time, dt.time]:
        return tuple(parse_clock(v) for v in (self.send_at, self.list_until, self.appeal_until, self.review_until))


@dataclass
class CameraApiConfig:
    url: str = ""  # Apps Script web app /exec URL; empty disables reporting
    key: str = field(default="", repr=False)  # CAMERA_SECRET, sent as secret in the JSON body
    include_late: bool = True
    include_not_seen: bool = True
    send_at: str = ""  # automatic reports only during this minute; empty uses camera window_end
    mode: str = "daily"
    days_back: int = 3  # previous calendar days; 0 means today only
    interval_seconds: int = 30
    interval_start: str = ""  # empty uses the first late minute
    interval_end: str = ""  # empty uses camera window_end


@dataclass
class LeaveApiConfig:
    """HR system API that provides holidays, extra work days and employee leave (see leave_api.py)."""
    url: str = ""  # empty: no leave data, only work_days decide who is expected
    token: str = ""  # sent as "Authorization: Bearer <token>"
    refresh_minutes: int = 15
    # Base requests still waiting for approval count as approved: someone who asked to come late or to be away is
    # not late or missing even before anybody approved it. A request refused later is not counted back by itself.
    pending_counts: bool = False
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
class CollectConfig:
    """Outside the camera hours, keep very clear faces of clearly recognised people as reference
    photos (see collect.py), so everyone's photos keep improving without anyone being asked."""
    enabled: bool = False
    start: str = "07:00"  # on work days, between these times, whenever the camera hours are not on
    end: str = "20:00"
    min_score: int = 65  # Image score (out of 100, as on the person page) a face needs
    per_day: int = 3  # photos kept per person per day, so one long chat at the door does not fill the gallery
    enough: int = 20  # a person with this many good photos is left alone
    fps: float = 2.0  # frames analysed per second while collecting: lighter on the computer than camera hours
    # A clear face the camera does not recognise (often someone with few reference photos) goes to
    # Review, at most this many a day; naming it there teaches the face, without recording an arrival.
    review_per_day: int = 10  # 0 = never
    # Employee ids (comma separated) nothing is collected for: someone who always wears a mask, whose masked
    # photos make other people's hidden or turned faces look like them.
    skip_people: str = ""
    # In the camera hours, with whole people followed: a turned face of someone the camera has just named goes
    # to Review with their name suggested (one per side and person a day); confirmed, it teaches that angle.
    angles: bool = True


@dataclass
class GreetingConfig:
    """The office Mac says hello at the door to each person recognised for the first time that day
    (see greeting.py). The sentences are written by Claude once in the morning."""
    enabled: bool = False
    key: str = field(default="", repr=False)  # Claude API key; empty: plain built-in greetings
    model: str = "claude-opus-5-5"
    engine: str = "macos"  # Vietnamese speech: macos, zerotts or elevenlabs
    # English speech: macos (voice_en), or zerotts with the same ZeroTTS voice as Vietnamese. ZeroTTS is
    # built for Vietnamese: it reads English with a Vietnamese accent, in one voice for both languages.
    engine_en: str = "macos"
    zerotts_voice: str = "maichi"
    zerotts_voices_dir: str = ""  # optional local directory containing custom <name>/voice.npz packs
    voice: str = "Linh"  # a macOS voice that speaks Vietnamese
    voice_en: str = "Samantha"  # the macOS voice for people greeted in English
    rate: int = 180  # words per minute
    # ZeroTTS only. speed: the audio is shortened after it is made, the pitch stays (1.0 = as made, 1.15 = a bit
    # brisker). liveliness: the model's audio sampling temperature; higher moves the voice up and down more, and
    # misreads words more often (0.8 is the model's own default).
    # For the people in until_people only (employee ids, comma separated), on work days: after the camera hours the
    # camera keeps looking until `until` ("HH:MM"), only to say hello to those of them who have not arrived yet
    # today. Nothing is recorded: no arrival, no photo, no review card. Everyone else is greeted in the camera hours.
    until: str = "12:00"
    until_people: str = ""
    zerotts_speed: float = 1.0
    zerotts_liveliness: float = 0.8
    # ElevenLabs (engine/engine_en "elevenlabs"): the sentence, with the names in it, is sent to api.elevenlabs.io
    # and the audio comes back; every character costs credits there. When it fails (no network, no credits left)
    # ZeroTTS reads instead. elevenlabs_prefix goes before every sentence, e.g. "[excited]" (an audio tag of v3).
    # Up to three accounts, tried in this order: when one's key is refused or its credits are used up, the next
    # reads. Each has its own voice ID (a voice belongs to an account); empty: the voice ID of the first.
    elevenlabs_key: str = field(default="", repr=False)
    elevenlabs_voice: str = "6Xq32rHt6oAu98TBDKfr"  # the voice ID from the ElevenLabs voice library (Tram)
    elevenlabs_voice_en: str = "ys3XeJJA4ArWMhRpcX1D"  # the voice for people greeted in English (Jess, Australian woman); empty: the same voice
    elevenlabs_key2: str = field(default="", repr=False)
    elevenlabs_voice2: str = ""
    elevenlabs_key3: str = field(default="", repr=False)
    elevenlabs_voice3: str = ""
    elevenlabs_model: str = "eleven_v3"
    elevenlabs_prefix: str = ""
    # With Eleven v3 the voice can act. emotions: Claude puts one of elevenlabs_tags (comma separated, without the
    # brackets) in square brackets before the part of a sentence it fits, e.g. "[surprised] Ủa, anh Minh!"; only
    # ElevenLabs is given the tags, everything shown or read by another voice is without them.
    elevenlabs_emotions: bool = True
    # Sounds (gasps, laughs...) and deliveries (excited, whispers...) the voice may be given, as Eleven v3 tags.
    elevenlabs_tags: str = ("gasps, laughs, chuckles, giggles, sighs happily, clears throat, applause, excited, "
                            "cheerfully, happily, playfully, surprised")
    elevenlabs_stability: float = 0.5  # 0 = most expressive, changes most between readings; 1 = most even
    elevenlabs_speed: float = 1.0  # 0.7 to 1.2, as ElevenLabs takes it
    # How much louder the door plays ZeroTTS and ElevenLabs audio than it was made (1.0 = as made, up to 3.0), for
    # a small speaker that is at full volume already: the quiet parts are raised, the peaks are rounded off, not cut.
    loudness: float = 1.0
    # The sound output the greetings should play on, as macOS names it ("External Headphones"); empty: any.
    # Playing on another device, a muted computer or a volume below speaker_min_volume (0 = not checked) is
    # told to the system space (see speaker.py).
    birthdays: bool = True  # someone whose birthday it is hears a birthday wish instead of the usual greeting
    speaker: str = ""
    speaker_min_volume: int = 30
    # A mains speaker that goes to sleep without a signal swallows a greeting, or its first words. On work
    # days at speaker_awake_from a soft low hum of a few seconds wakes it (it can be heard); from then until
    # the door stops greeting, whenever the speaker had no sound for speaker_awake_every minutes, a 20 Hz tone
    # nobody hears is played. speaker_awake_level: percent of full scale, 1-50, of the hum.
    speaker_awake: bool = False
    speaker_awake_from: str = "07:30"
    speaker_awake_every: int = 120
    speaker_awake_level: int = 6
    # Write and voice the day's sentences on days off too (weekends, holidays). Off: only on work days, so a
    # day nobody is expected costs no Claude or ElevenLabs credits; anyone who comes hears a plain hello.
    days_off: bool = False
    # A person who arrives this many seconds after the last greeting ended, or while someone else waits, hears their
    # short hello: people coming in one after another are all greeted before they walk on. 0: only while someone waits.
    busy_seconds: int = 8
    # How the door calls someone whose name or email has no accents: "who = name", one per line or comma separated;
    # who is their email, its first part or their full name ("tan.nguyen@offspringdigital.com = Tân").
    call_names: str = ""
    style: str = ""  # extra wishes for the sentences, e.g. "mention the weather" (any language)
    # The office, for the morning weather: coordinates as Google Maps gives them ("21.0285, 105.8542", or the
    # address of a place there: kept as "lat,lon"), or a place name ("Hà Nội"); empty: no weather.
    location: str = ""
    # What is in fashion to say right now (catchphrases, memes, a song everyone hums), one per line: Claude works one
    # into about one greeting in four, where it fits. Empty: none.
    trends: str = ""
    # Claude looks up on the web, once a week, what is in fashion to say in Vietnam right now (workplace-safe
    # catchphrases and memes), and uses those too. Needs the Claude key; costs a few web searches a week.
    trends_research: bool = True
    # Facts about the office Claude keeps to, so it invents nothing ("no printer", "lunch together at 12"), any language.
    office: str = ""
    unknown: bool = True  # a short hello without a name for a face the camera cannot name (at most one every 2 minutes)
    # Jokes about how someone has arrived lately ("often late lately", "on time 6 work days in a row"): that label,
    # never a time, is sent to Claude with the name. Off: nothing about anyone's attendance leaves the office.
    tease: bool = True


@dataclass
class EdgeConfig:
    """The office Mac mirrors camera events to the server at this address. Empty: no server."""
    server_url: str = ""
    token: str = field(default="", repr=False)


@dataclass
class Config:
    timezone: str = "Asia/Ho_Chi_Minh"
    data_dir: str = "data"
    employees_file: str = "employees.csv"  # email,name CSV, imported initially or once to attach legacy emails
    healthcheck_url: str = ""
    base_api_key: str = field(default='', repr=False)  # Base Timeoff V2; BASE_ACCESS_TOKEN overrides this
    # Base Account V2 token, for birthdays only (see integrations/base_account.py). It can read and change
    # accounts: it stays on the server, and only the day and month of birth are taken.
    base_account_key: str = field(default='', repr=False)
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
    camera_api: CameraApiConfig = field(default_factory=CameraApiConfig)
    late_message: LateMessageConfig = field(default_factory=LateMessageConfig)
    pending_reminder: PendingReminderConfig = field(default_factory=PendingReminderConfig)
    fine_notice: FineNoticeConfig = field(default_factory=FineNoticeConfig)
    web: WebConfig = field(default_factory=WebConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    collect: CollectConfig = field(default_factory=CollectConfig)
    edge: EdgeConfig = field(default_factory=EdgeConfig)
    greeting: GreetingConfig = field(default_factory=GreetingConfig)
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
        until = str(self.schedule.maintenance_until or "").strip()
        try:
            self.schedule.maintenance_until = dt.datetime.fromisoformat(until).strftime("%Y-%m-%dT%H:%M") if until else ""
        except ValueError:
            raise ValueError("schedule.maintenance_until must be a date and time like 2026-11-21T17:00") from None
        if self.recognition.backend not in DEFAULT_MODELS:
            raise ValueError(f"recognition.backend must be one of {sorted(DEFAULT_MODELS)}")
        if any(d not in range(7) for d in self.schedule.work_days):
            raise ValueError("work_days must contain numbers 0 (Monday) to 6 (Sunday)")
        r = self.recognition
        if not (0 < r.accept_score < 1 and 0 <= r.min_margin < 1 and 0 <= r.min_det_score <= 1):
            raise ValueError("accept_score, min_margin and min_det_score must be between 0 and 1")
        if r.votes_required < 1 or r.unknown_min_frames < 1 or r.min_face_px < 1:
            raise ValueError("votes_required, unknown_min_frames and min_face_px must be at least 1")
        if not 20 <= r.small_face_px <= r.min_face_px:
            raise ValueError("small_face_px must be between 20 and min_face_px")
        if r.learn_per_group < 1 or r.group_days < 1:
            raise ValueError("learn_per_group and group_days must be at least 1")
        if not 0 <= r.seen_again_score < 1:
            raise ValueError("seen_again_score must be between 0 and 1")
        if not 0 < r.suggest_score < 1:
            raise ValueError("suggest_score must be between 0 and 1")
        if not 0 <= r.same_face_score < 1:
            raise ValueError("same_face_score must be between 0 and 1")
        if not 0 <= r.blurry_score <= 100:
            raise ValueError("blurry_score must be between 0 and 100")
        if not 0 <= r.auto_delete_photos_below <= 100:
            raise ValueError("auto_delete_photos_below must be between 0 and 100")
        if not 0 < self.camera.process_fps <= 30:
            raise ValueError("process_fps must be between 0 and 30")
        if not 0 <= self.camera.decode_threads <= 32:
            raise ValueError("camera.decode_threads must be between 0 (FFmpeg's choice) and 32")
        urls = self.camera.urls()
        if len(urls) > MAX_CAMERAS:
            raise ValueError(f"at most {MAX_CAMERAS} cameras: each one costs as much CPU as the first")
        if len(set(urls)) != len(urls):
            raise ValueError("the same camera address is listed twice")
        if r.det_size < 160 or (r.backend == "insightface" and r.det_size % 32):
            raise ValueError("det_size must be at least 160 and, for insightface, a multiple of 32 (e.g. 640, 960)")
        if self.retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if self.late_evidence_days < 1:
            raise ValueError("late_evidence_days must be at least 1")
        self.web.viewer_username = self.web.viewer_username.strip()
        if self.web.viewer_password and (not self.web.viewer_username or self.web.viewer_username == self.web.username):
            raise ValueError("the read-only login needs its own user name, different from the admin's")
        pr = self.pending_reminder
        pr.time = parse_clock(pr.time).strftime("%H:%M")
        pr.kinds = ",".join(k for k in (v.strip() for v in pr.kinds.split(",")) if k)
        from ..integrations.base_timeoff import KINDS
        if any(k not in KINDS for k in pr.kinds.split(",") if k):
            raise ValueError(f"pending_reminder.kinds must be Base type ids from {sorted(KINDS)}")
        self.backup.folder = self.backup.folder.strip()
        self.backup.time = parse_clock(self.backup.time).strftime("%H:%M")
        if self.backup.keep_days < 1:
            raise ValueError("backup.keep_days must be at least 1")
        c = self.collect
        c.start, c.end = (parse_clock(t).strftime("%H:%M") for t in (c.start, c.end))
        if c.start >= c.end:
            raise ValueError("collect.start must be before collect.end")
        if not 1 <= c.min_score <= 100:
            raise ValueError("collect.min_score must be between 1 and 100")
        if c.review_per_day < 0:
            raise ValueError("collect.review_per_day must not be negative")
        if c.per_day < 1 or c.enough < 1:
            raise ValueError("collect.per_day and collect.enough must be at least 1")
        if not 0.2 <= c.fps <= 30:
            raise ValueError("collect.fps must be between 0.2 and 30")
        g = self.greeting
        g.engine = g.engine.strip().lower()
        if g.engine not in ("macos", "zerotts", "elevenlabs"):
            raise ValueError("greeting.engine must be macos, zerotts or elevenlabs")
        g.engine_en = g.engine_en.strip().lower()
        if g.engine_en not in ("macos", "zerotts", "elevenlabs"):
            raise ValueError("greeting.engine_en must be macos, zerotts or elevenlabs")
        g.elevenlabs_voice_en = g.elevenlabs_voice_en.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{0,64}", g.elevenlabs_voice_en):
            raise ValueError("greeting.elevenlabs_voice_en must be a voice ID (letters and numbers)")
        for n in ("", "2", "3"):
            setattr(g, f"elevenlabs_key{n}", getattr(g, f"elevenlabs_key{n}").strip())
            voice = getattr(g, f"elevenlabs_voice{n}").strip()
            setattr(g, f"elevenlabs_voice{n}", voice)
            if not re.fullmatch(r"[A-Za-z0-9]{0,64}", voice):
                raise ValueError(f"greeting.elevenlabs_voice{n} must be a voice ID (letters and numbers)")
        g.elevenlabs_model, g.elevenlabs_prefix = g.elevenlabs_model.strip(), g.elevenlabs_prefix.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", g.elevenlabs_model):
            raise ValueError("greeting.elevenlabs_model must be a model ID such as eleven_v3")
        if len(g.elevenlabs_prefix) > 60:
            raise ValueError("greeting.elevenlabs_prefix must be at most 60 characters")
        g.elevenlabs_stability, g.elevenlabs_speed = float(g.elevenlabs_stability), float(g.elevenlabs_speed)
        g.loudness = float(g.loudness)
        if not 1.0 <= g.loudness <= 3.0:
            raise ValueError("greeting.loudness must be between 1 and 3")
        g.speaker = " ".join(str(g.speaker).split())
        if len(g.speaker) > 80:
            raise ValueError("greeting.speaker must be at most 80 characters")
        g.speaker_min_volume = int(g.speaker_min_volume)
        if not 0 <= g.speaker_min_volume <= 100:
            raise ValueError("greeting.speaker_min_volume must be between 0 and 100")
        g.speaker_awake_from = parse_clock(g.speaker_awake_from).strftime("%H:%M")
        g.speaker_awake_every, g.speaker_awake_level = int(g.speaker_awake_every), int(g.speaker_awake_level)
        if not 1 <= g.speaker_awake_every <= 480:
            raise ValueError("greeting.speaker_awake_every must be between 1 and 480 minutes")
        g.busy_seconds = int(g.busy_seconds)
        if not 0 <= g.busy_seconds <= 60:
            raise ValueError("greeting.busy_seconds must be between 0 and 60")
        if not 1 <= g.speaker_awake_level <= 50:
            raise ValueError("greeting.speaker_awake_level must be between 1 and 50")
        tags = list(dict.fromkeys(" ".join(t.strip(" []").lower().split()) for t in str(g.elevenlabs_tags).split(",")))
        tags = [t for t in tags if t]
        if len(tags) > 12 or not all(re.fullmatch(r"[a-z][a-z ]{0,23}", t) for t in tags):
            raise ValueError("greeting.elevenlabs_tags must be at most 12 English words or short phrases, comma separated")
        g.elevenlabs_tags = ", ".join(tags)
        if not 0.0 <= g.elevenlabs_stability <= 1.0:
            raise ValueError("greeting.elevenlabs_stability must be between 0 and 1")
        if not 0.7 <= g.elevenlabs_speed <= 1.2:
            raise ValueError("greeting.elevenlabs_speed must be between 0.7 and 1.2")
        g.zerotts_voice = g.zerotts_voice.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", g.zerotts_voice):
            raise ValueError("greeting.zerotts_voice must be a voice name (letters, numbers, _, - or .)")
        g.zerotts_voices_dir = g.zerotts_voices_dir.strip()
        g.key = g.key.strip()
        g.model, g.voice, g.style = g.model.strip() or "claude-opus-5-5", g.voice.strip() or "Linh", g.style.strip()[:300]
        g.voice_en = g.voice_en.strip() or "Samantha"
        from ..integrations.weather import coordinates

        g.location = (coordinates(g.location) or g.location.strip())[:300]
        g.trends = "\n".join(line.strip() for line in str(g.trends).splitlines() if line.strip())[:800]
        g.office = "\n".join(line.strip() for line in str(g.office).splitlines() if line.strip())[:600]
        if not 90 <= g.rate <= 360:
            raise ValueError("greeting.rate must be between 90 and 360 words per minute")
        g.until = str(g.until or "").strip()
        g.until_people = ",".join(dict.fromkeys(v.strip() for v in str(g.until_people).split(",") if v.strip()))
        self.collect.skip_people = ",".join(dict.fromkeys(v.strip() for v in str(self.collect.skip_people).split(",") if v.strip()))
        if g.until:
            g.until = parse_clock(g.until).strftime("%H:%M")
        g.zerotts_speed, g.zerotts_liveliness = round(float(g.zerotts_speed), 2), round(float(g.zerotts_liveliness), 2)
        if not 0.8 <= g.zerotts_speed <= 1.5:
            raise ValueError("greeting.zerotts_speed must be between 0.8 and 1.5")
        if not 0.5 <= g.zerotts_liveliness <= 1.2:
            raise ValueError("greeting.zerotts_liveliness must be between 0.5 and 1.2")
        lm = self.late_message
        if str(lm.send_at).strip():
            lm.send_at = parse_clock(lm.send_at).strftime("%H:%M")
            # At the late time itself nobody has come late yet, so the message would be empty.
            if parse_clock(lm.send_at) <= late:
                raise ValueError("the late message must be sent after the late time")
        else:
            lm.send_at = ""
        check_late_message_item(lm.item)
        lm.space = str(lm.space).strip().removeprefix("spaces/")
        if lm.space and not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", lm.space):
            raise ValueError("late_message.space must be a Google Chat space ID such as AAAAabc123")
        fn = self.fine_notice
        fn.send_at, fn.list_until, fn.appeal_until, fn.review_until = (c.strftime("%H:%M") for c in fn.clocks())
        if not fn.send_at < fn.list_until <= fn.appeal_until <= fn.review_until:
            raise ValueError("fine notice times must be in order: send < list closes <= appeals close <= review closes")
        fn.approvers = ",".join(dict.fromkeys(v.strip() for v in str(fn.approvers).split(",") if v.strip()))
        fn.approver_webhook_url = fn.approver_webhook_url.strip()
        if fn.approver_webhook_url and not fn.approver_webhook_url.startswith("https://"):
            raise ValueError("the approvers' webhook must be an https address")
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
        if self.base_api_key and self.leave_api.url.strip():
            raise ValueError('configure Base Timeoff or the generic leave_api, not both')
        if self.google_chat.unknown_batch_minutes < 0:
            raise ValueError("unknown_batch_minutes must not be negative")
        self.camera_api.url = self.camera_api.url.strip()
        self.camera_api.key = self.camera_api.key.strip()
        if self.camera_api.mode not in ("daily", "interval"):
            raise ValueError("camera_api.mode must be daily or interval")
        if type(self.camera_api.days_back) is not int or not 0 <= self.camera_api.days_back <= 3:
            raise ValueError("camera_api.days_back must be an integer between 0 and 3")
        interval = self.camera_api.interval_seconds
        if type(interval) is not int or not 30 <= interval <= 86400 or interval % 30:
            raise ValueError("camera_api.interval_seconds must be a multiple of 30 between 30 and 86400")
        for name in ("interval_start", "interval_end"):
            value = getattr(self.camera_api, name)
            try:
                setattr(self.camera_api, name, parse_clock(value).strftime("%H:%M") if str(value).strip() else "")
            except (TypeError, ValueError):
                raise ValueError(f"camera_api.{name} must be HH:MM") from None
        if self.camera_api.mode == "interval":
            interval_start = (parse_clock(self.camera_api.interval_start) if self.camera_api.interval_start else
                              (dt.datetime.combine(dt.date.min, late) + dt.timedelta(minutes=1)).time())
            interval_end = parse_clock(self.camera_api.interval_end) if self.camera_api.interval_end else end
            if not late < interval_start < interval_end:
                raise ValueError("camera_api interval must start after On time through and end after its start on the same day")
        if str(self.camera_api.send_at).strip():
            try:
                send_time = parse_clock(self.camera_api.send_at)
            except (TypeError, ValueError):
                raise ValueError("camera_api.send_at must be HH:MM") from None
            if send_time <= late:
                raise ValueError("camera_api.send_at must be after schedule.late_after (On time through)")
            self.camera_api.send_at = send_time.strftime("%H:%M")
        else:
            self.camera_api.send_at = ""
        url = urlsplit(self.camera_api.url)
        if self.camera_api.url and (url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment):
            raise ValueError("camera_api.url must be an HTTPS server URL without credentials or fragment")
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
    cfg.base_api_key = os.environ.get('BASE_ACCESS_TOKEN', cfg.base_api_key).strip()
    cfg.base_account_key = str(cfg.base_account_key or '').strip()
    cfg.base_dir = path.resolve().parent
    cfg.validate()
    return cfg
