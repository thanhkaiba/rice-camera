"""First-run setup: a short guided flow (camera, working hours, Google Chat), then a checklist of
what is configured and what is left to do."""
from __future__ import annotations

import socket
from dataclasses import dataclass

DISMISSED_KEY = "setup_dismissed"
SKIP_EMPLOYEES_KEY = "setup_skip_employees"
HOURS_KEY = "setup_hours_done"
SKIP_CHAT_KEY = "setup_skip_chat"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")
WIZARD = ["camera", "hours", "chat", "done"]


def wizard_step(core) -> str:
    """The first step of the guided flow that is not done yet."""
    cfg, store = core.cfg, core.store
    if not cfg.camera.url:
        return "camera"
    if store.get(HOURS_KEY) != "1":
        return "hours"
    if not cfg.google_chat.webhook_url and store.get(SKIP_CHAT_KEY) != "1":
        return "chat"
    return "done"


@dataclass
class Step:
    key: str
    title: str
    done: bool
    required: bool
    detail: str = ""


def lan_address(port: int) -> str:
    """This computer's address on the office network, for links in chat messages."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # UDP: no packet is sent; this only picks the outgoing interface
            ip = s.getsockname()[0]
    except OSError:
        ip = "localhost"
    return f"http://{ip}:{port}"


def steps(core) -> list[Step]:
    cfg, store, live = core.cfg, core.store, core.live
    employees = store.employee_count()
    with_photos = len([e for e in store.employees() if core.gallery.counts().get(e)])
    with_chat = len([e for e in store.all_employees() if e.active and e.chat_user])
    skip_employees = store.get(SKIP_EMPLOYEES_KEY) == "1"
    camera_detail = ""
    if cfg.camera.url:
        if live.camera_state == "ok":
            camera_detail = "Picture received."
        elif live.camera_state.startswith("off"):
            camera_detail = "Click Check the picture to see what the camera sees."
        else:
            camera_detail = f"Camera status: {live.camera_state}."
    return [
        Step("camera", "Camera address", bool(cfg.camera.url), True, camera_detail),
        Step("password", "Password for this page", bool(cfg.web.password), True,
             f"User name: {cfg.web.username}" if cfg.web.password else "Anyone on the office network can open it now."),
        Step("address", "Address of this page", not any(h in cfg.web.public_url for h in LOCAL_HOSTS), True,
             "Used for the links in Google Chat messages."),
        Step("chat", "Google Chat", bool(cfg.google_chat.webhook_url), True),
        Step("employees", "Employees", employees > 0 or skip_employees, True,
             f"{employees} employees, {with_photos} with photos." if employees else
             ("The camera collects faces; name them on the Review page." if skip_employees else "")),
        Step("area", "Detection area", bool(cfg.camera.area), False),
        Step("mentions", "@mentions in Google Chat", employees > 0 and with_chat == employees, False,
             f"{with_chat}/{employees} employees have a Google Chat ID." if employees else ""),
        Step("hr", "Holidays & leave from the HR system", bool(cfg.leave_api.url), False),
        Step("backup", "Daily backup to another place", bool(cfg.backup.folder), False,
             "Settings → Access & privacy → Backup: a folder on an external drive, a network share or a cloud-synced folder."),
    ]


def first_run(core) -> bool:
    """A fresh install: no camera and nobody entered yet, and setup not put off."""
    return (not core.cfg.camera.url and core.store.employee_count() == 0
            and core.store.get(DISMISSED_KEY) != "1")


def open_count(core) -> int:
    return sum(1 for s in steps(core) if s.required and not s.done)
