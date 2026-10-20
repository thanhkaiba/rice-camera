"""Application wiring and the main loop: camera -> faces -> tracker -> attendance service."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import urllib.request
from pathlib import Path

from . import settings
from .backup import Backup
from .config import Config
from .evidence import evidence_digest
from .machine import MachineWatch
from .placement import Placement
from ..vision.faces import FaceLibrary
from ..vision.gallery import Gallery
from ..integrations.leave_api import LeaveSync
from ..vision.live import FrameHealth, LiveStatus
from ..vision.posecal import PoseCalibration
from ..integrations.notifier import GoogleChat
from .service import AttendanceService
from ..store import Store, body_photo, scene_photo
from ..vision.tracker import Decision, vote_for

log = logging.getLogger(__name__)

# A camera that sends nothing for this long is reopened: a dropped RTSP connection can leave
# the decoder waiting forever without ever reporting an error.
CAMERA_RESTART_S = 60.0


class App:
    """Everything the camera loop, the web page and the command line share."""

    def __init__(self, cfg: Config, now=None):
        self.cfg = cfg
        self.data_dir = cfg.path(cfg.data_dir)
        self.store = Store(self.data_dir / "attendance.db")
        settings.load(cfg, self.store)
        employees_file = cfg.path(cfg.employees_file)
        if employees_file.exists() and (self.store.employee_count() == 0
                                       or (not self.store.get("employee_email_csv_imported")
                                           and any(not p.email for p in self.store.all_employees()))):
            try:
                n = self.store.import_employees(employees_file.read_text(encoding="utf-8-sig"))
            except ValueError as exc:
                log.warning("Employee import skipped: %s. Update the CSV or assign emails in People.", exc)
            else:
                self.store.put("employee_email_csv_imported", "1")
                log.info("Imported %d employees from %s", n, employees_file)
        self.gallery = Gallery(self.store.embeddings(cfg.recognition.model_id()), cfg.recognition.center_embeddings)
        self.chat = GoogleChat(lambda: cfg.google_chat.webhook_url, self.store)
        self.service = AttendanceService(cfg, self.store, self.chat, self.gallery, now=now)
        self.recognizer_lock = threading.Lock()
        self._recognizer = None
        self.library = FaceLibrary(cfg, self.store, self.gallery, self.recognizer, self.recognizer_lock,
                                   alert=self.service.alert, now=self.service.now)
        self.service.library = self.library
        self.leave_sync = LeaveSync(cfg, self.store, self.service.now, lambda d: self.service.window(d).late_after)
        self.live = LiveStatus()
        self.maintenance: str | None = None  # e.g. a seed import: the camera pauses while it is set
        self.pose = PoseCalibration(self.store, lambda: cfg.recognition.auto_pose)
        self.backup = Backup(self.store, cfg, self.service.now, self.service.alert)
        self.service.backup = self.backup
        self.placement = Placement(self.store)
        self.service.placement = self.placement
        self.machine = MachineWatch(cfg, self.store, self.service.alert, self.service.now)

    def recognizer(self):
        if self._recognizer is None:
            from ..vision.recognizer import FaceRecognizer

            self._recognizer = FaceRecognizer(self.cfg.recognition, self.cfg.base_dir)
        return self._recognizer


def save_crop(app: App, decision: Decision, ts: dt.datetime) -> str | None:
    if decision.crop is None:
        return None
    from ..vision.faces import encode_jpeg

    who = decision.employee_id if decision.kind == "identified" else "unknown"
    rel = Path("crops") / ts.strftime("%Y-%m-%d") / f"{ts:%H%M%S_%f}_{who}.jpg"
    path = app.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_jpeg(decision.crop))
    if decision.body is not None:
        (app.data_dir / body_photo(rel.as_posix())).write_bytes(encode_jpeg(decision.body))
    if decision.scene is not None:
        if decision.kind == "identified":
            label = f"{app.store.employees().get(who, who)}  match {decision.score or 0:.2f}"
        else:
            label = "unidentified face"
        frame, bbox = decision.scene
        (app.data_dir / scene_photo(rel.as_posix())).write_bytes(
            render_scene(frame, bbox, f"{ts:%Y-%m-%d %H:%M:%S} ({ts:%z})  {label}"))
    return rel.as_posix()


def render_scene(frame, bbox, label: str, max_width: int = 1280) -> bytes:
    """The whole camera picture with the face boxed and `label` (time, name) burned in, so the
    picture is evidence on its own, without the database."""
    import cv2

    from ..vision.faces import encode_jpeg

    canvas = frame.copy()
    h, w = canvas.shape[:2]
    scale = min(1.0, max_width / w)
    if scale < 1:
        canvas = cv2.resize(canvas, (max_width, int(h * scale)))
    x1, y1, x2, y2 = (int(v * scale) for v in bbox)
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 215, 255), 2)
    size = max(0.5, canvas.shape[1] / 1600)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, size, 1)
    cv2.rectangle(canvas, (0, 0), (tw + 16, th + 16), (0, 0, 0), -1)
    cv2.putText(canvas, label, (8, th + 8), cv2.FONT_HERSHEY_SIMPLEX, size, (255, 255, 255), 1, cv2.LINE_AA)
    return encode_jpeg(canvas)


def _healthcheck(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as exc:
        log.warning("Healthcheck ping failed: %s", exc)


def _leave_sync_loop(app: App) -> None:
    """Refresh holidays and leave from the HR system in the background (network calls can be slow)."""
    while True:
        try:
            if app.leave_sync.due():
                app.leave_sync.sync()
        except Exception:
            log.exception("Leave data update crashed")
        time.sleep(30)


def face_label(app: App, face, candidates) -> tuple[str, tuple[int, int, int]]:
    """Text and BGR colour drawn on the live view for one face."""
    rc = app.cfg.recognition
    name = app.service.chat_name  # the email name by default, as in Chat and on Review
    person = vote_for(candidates, face.quality_ok, face.size_ok, rc)
    if person is not None:
        return f"{name(person)} {candidates[0][1]:.2f}", (60, 180, 60)
    if face.size < rc.min_face_px:
        reason = "too small"
    elif face.sharpness < rc.min_sharpness:
        reason = "blurred"
    elif abs(face.yaw) > rc.max_yaw or abs(face.pitch) > rc.max_pitch:
        reason = "turned away"
    elif face.det_score < rc.vote_det_score:
        reason = "unclear"  # followed and sent for review, but too uncertain to count
    else:
        best = f" (closest {name(candidates[0][0])} {candidates[0][1]:.2f})" if candidates else ""
        return f"no match{best}", (40, 40, 220)
    return f"? {reason}", (0, 165, 255)


PREVIEW_WIDTH = 1600  # the Camera page picture; 960 px made a 2880 px camera look blurred
PREVIEW_FPS = 3.0  # pictures drawn for the Camera page per second, however fast frames are analysed


def draw(frame, labelled, people=(), area=None) -> bytes:
    import cv2

    from ..vision.faces import encode_jpeg

    canvas = frame.copy()
    h, w = canvas.shape[:2]
    line = max(2, round(2 * w / PREVIEW_WIDTH))  # lines and labels keep their size once scaled down
    font = 0.6 * max(1.0, w / PREVIEW_WIDTH)
    if area is not None:  # dim what is not analysed and outline the area
        x1, y1, x2, y2 = area
        inside = canvas[y1:y2, x1:x2].copy()
        canvas = (canvas * 0.45).astype(canvas.dtype)
        canvas[y1:y2, x1:x2] = inside
        cv2.rectangle(canvas, (x1, y1), (x2 - 1, y2 - 1), (0, 215, 255), line)
    for p in people:  # person tracking: thin grey box with the person's track number
        x1, y1, x2, y2 = (int(v) for v in p.box)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (170, 170, 170), max(1, line // 2))
        cv2.putText(canvas, f"#{p.id}", (x1 + 4, y2 - 6), cv2.FONT_HERSHEY_SIMPLEX, font * 0.8, (170, 170, 170),
                    max(1, line // 2))
    for face, (text, colour) in labelled:
        x1, y1, x2, y2 = (int(v) for v in face.bbox)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, line)
        cv2.putText(canvas, text, (x1, max(int(30 * font), y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, font, colour, line)
    if w > PREVIEW_WIDTH:
        canvas = cv2.resize(canvas, (PREVIEW_WIDTH, int(h * PREVIEW_WIDTH / w)), interpolation=cv2.INTER_AREA)
    return encode_jpeg(canvas)


def _grow(bbox, shape, by: float = 0.5):
    """A face box made larger, so the face is still inside it a fraction of a second later."""
    x1, y1, x2, y2 = bbox
    dx, dy = (x2 - x1) * by, (y2 - y1) * by
    return max(0, x1 - dx), max(0, y1 - dy), min(shape[1], x2 + dx), min(shape[0], y2 + dy)


def fetch_models(app: App, retry_s: float = 60.0) -> None:
    """Download missing models on the first start (the installer packages carry none), showing
    progress on the web page. Waits and retries while there is no internet connection."""
    from ..vision import models

    live = app.live
    if not models.missing(app.cfg):
        return

    def progress(done: int, total: int) -> None:
        live.models = (f"Downloading the face recognition models: {done / total:.0%} of {total >> 20} MB"
                       if total else f"Downloading the face recognition models: {done >> 20} MB")

    while True:
        live.models = "Downloading the face recognition models…"
        try:
            models.ensure(app.cfg, progress)
            live.models = None
            log.info("Models downloaded to %s", app.cfg.path("models"))
            return
        except Exception as exc:
            live.models = (f"Could not download the face recognition models ({exc}). "
                           f"Check the internet connection; trying again in a minute")
            log.error(live.models)
            time.sleep(retry_s)


def run(cfg: Config) -> None:
    from ..vision.camera import Camera
    from ..vision.processing import FrameProcessor
    from ..vision.recognizer import enhance_low_light

    app = App(cfg)
    service, live = app.service, app.live
    app.chat.start_background()  # a slow webhook must not hold up face recognition
    if cfg.web.enabled:
        from ..web import create_app

        web = create_app(app)
        threading.Thread(target=lambda: web.run(host=cfg.web.host, port=cfg.web.port, threaded=True),
                         name="web", daemon=True).start()
        log.info("Admin page on %s", cfg.web.public_url)

    threading.Thread(target=_leave_sync_loop, args=(app,), name="leave-sync", daemon=True).start()

    fetch_models(app)
    recognizer = None
    try:
        recognizer = app.recognizer()
        app.library.reembed_missing()
        app.library.rate_in_background = True
        app.library.rate_soon()
    except Exception as exc:
        live.problem = f"Face models could not be loaded: {exc}"
        log.error(live.problem)

    missing = [e for e in app.store.employees() if e not in app.gallery.counts()]
    if missing:
        log.warning("No reference photos for: %s", ", ".join(missing))

    processor = FrameProcessor(app)
    face_regions: list = []  # where faces were in the last analysed frame
    health = FrameHealth()
    camera: Camera | None = None
    last_processed = last_chosen = last_ping = last_tick = last_recorded = last_drawn = 0.0
    placement_saved = time.time()
    fps_count, fps_since = 0, time.time()
    was_in_window = False

    def handle(decisions: list[Decision]) -> None:
        for d in decisions:
            ts = d.ts.astimezone(service.tz)
            image = save_crop(app, d, ts)
            digest = evidence_digest(app.data_dir, image)
            if d.kind == "identified":
                service.on_identified(d.employee_id, ts, d.score, image, d.embedding, digest)
                live.last_identified = f"{app.service.chat_name(d.employee_id)} at {ts:%H:%M:%S}"
            else:
                service.on_unknown(ts, image, d.embedding, d.employee_id, d.score, digest)

    log.info("Running; processing window %s-%s", cfg.schedule.window_start, cfg.schedule.window_end)
    while True:
        now = service.now()
        in_window = service.in_window(now) and not app.maintenance
        if was_in_window and not in_window:
            handle(processor.flush())
            app.placement.save()  # before the summary, which reads it
        was_in_window = in_window
        camera_ok = True

        if (in_window or live.previewing) and recognizer is not None and cfg.camera.url and not app.maintenance:
            if (camera is None or camera.source != cfg.camera.url or camera.silent_for() > CAMERA_RESTART_S
                    or camera.decode_threads != cfg.camera.decode_threads):
                if camera is not None:
                    if camera.source == cfg.camera.url and camera.silent_for() > CAMERA_RESTART_S:
                        log.warning("No frames for %.0fs, reopening the camera", camera.silent_for())
                    camera.stop(wait_s=1.0)  # do not let a hung reader hold up the loop
                camera = Camera(cfg.camera.url, cfg.camera.decode_threads).start()
                live.camera_state = "connecting"
            frame, frame_time = camera.latest()
            camera_ok = frame is not None
            if frame is None:
                live.camera_state = "no picture (reconnecting)" if camera.started_for() > 10 else "connecting"
                time.sleep(0.05)
            elif frame_time - last_processed >= 1.0 / cfg.camera.process_fps:
                last_processed = frame_time  # paces the analysis on the newest frame
                frame, frame_time = camera.sharpest(last_chosen, face_regions)
                last_chosen = frame_time
                problem = health.update(frame, frame_time)
                camera_ok = problem is None
                live.camera_state = f"problem: {problem}" if problem else "ok"
                if cfg.camera.enhance_low_light:
                    frame = enhance_low_light(frame)
                ts = dt.datetime.fromtimestamp(frame_time, service.tz)
                decisions, matched, people = processor.process(frame, ts, record=in_window)
                handle(decisions)
                if in_window:
                    app.placement.add_frame(ts.date(), [f for f, _, _ in matched], cfg.recognition,
                                            frame_time - last_recorded)
                    last_recorded = frame_time
                if processor.person_error and live.problem is None:
                    live.problem = processor.person_error
                face_regions = [_grow(f.bbox, frame.shape) for f, _, _ in matched]
                labelled = [(f, face_label(app, f, c)) for f, c, _ in matched]
                live.faces = [text for _, (text, _) in labelled]
                live.people = len(people)
                if live.wants_frame() and frame_time - last_drawn >= 1.0 / PREVIEW_FPS:
                    last_drawn = frame_time
                    live.set_frame(draw(frame, labelled, people, cfg.camera.area_box(frame.shape)), frame)
                fps_count += 1
            else:
                time.sleep(0.02)
        else:
            if camera is not None:
                camera.stop()
                camera = None
            live.camera_state = (f"paused ({app.maintenance})" if app.maintenance
                                 else "off (outside the camera hours)" if cfg.camera.url
                                 else "off (no camera address in Settings)")
            if in_window or (app.maintenance and service.in_window(now)):
                camera_ok = False  # should be working but cannot: count it as an outage
            time.sleep(0.5)

        if time.time() - fps_since >= 5:
            live.fps, fps_count, fps_since = fps_count / (time.time() - fps_since), 0, time.time()
            live.source_fps = camera.received_fps() if camera is not None else 0.0
        if time.time() - placement_saved >= 60:
            placement_saved = time.time()
            app.placement.save()
        app.machine.check_soon()
        if time.time() - last_tick >= 1.0:
            last_tick = time.time()
            service.tick(camera_ok, live.camera_state)
        if cfg.healthcheck_url and time.time() - last_ping >= 60:
            last_ping = time.time()
            threading.Thread(target=_healthcheck, args=(cfg.healthcheck_url,), daemon=True).start()
