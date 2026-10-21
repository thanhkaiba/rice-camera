"""Check a camera address while it is being entered: can the camera be reached, and does it send a picture?

Two levels, so the page can say what to fix:
- "warn": nothing answers at the address's IP and port (wrong IP, camera off, other network).
- "error": something answers but no picture comes (wrong user name, password or stream path).
"""
from __future__ import annotations

import base64
import socket
import threading
from urllib.parse import urlparse

DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}
CONNECT_TIMEOUT_S = 3
PICTURE_TIMEOUT_S = 10


def reachable(host: str, port: int, timeout_s: float = CONNECT_TIMEOUT_S) -> str | None:
    """None if a TCP connection to host:port works, else why not."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return None
    except socket.gaierror:
        return f"the name {host} is not known"
    except TimeoutError:
        return "no answer"
    except OSError as exc:
        return exc.strerror or str(exc)


def grab_frame(source, timeout_s: float = PICTURE_TIMEOUT_S):
    """One frame from the camera, or None. Gives up after timeout_s even if OpenCV hangs."""
    import cv2

    result: list = []

    def read() -> None:
        if isinstance(source, int):
            cap = cv2.VideoCapture(source)
        else:
            ms = int(timeout_s * 1000)
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG,
                                   [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, ms])
        try:
            ok, frame = cap.read() if cap.isOpened() else (False, None)
            result.append(frame if ok else None)
        finally:
            cap.release()

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout_s + 2)
    return result[0] if result else None


def check(url: str) -> dict:
    """{"level": "ok" | "warn" | "error", "message": ..., "image": data URL of a small snapshot (ok only)}."""
    url = url.strip()
    if not url:
        return {"level": "error", "message": "Enter the camera address."}
    source: int | str = int(url) if url.isdigit() else url
    if isinstance(source, str) and "://" in source:
        parsed = urlparse(source)
        scheme = parsed.scheme.lower()
        try:
            port = parsed.port or DEFAULT_PORTS.get(scheme)
        except ValueError:
            return {"level": "error", "message": "The port in the address is not a number."}
        if not parsed.hostname or port is None:
            return {"level": "error", "message": "The address needs an IP, e.g. rtsp://admin:CODE@192.168.1.64:554/…"}
        why = reachable(parsed.hostname, port)
        if why:
            return {"level": "warn", "message": f"Cannot reach {parsed.hostname}:{port} ({why}). Check that the camera "
                                                f"is on, on the same network as this computer, and that the IP and port are right."}
    frame = grab_frame(source)
    if frame is None:
        what = "The camera answers" if isinstance(source, str) and "://" in source else "The camera was found"
        return {"level": "error", "message": f"{what} but sends no picture. Check the user name, password and the "
                                             f"stream path (Ezviz/Hikvision: /Streaming/Channels/101)."}
    import cv2

    h, w = frame.shape[:2]
    scale = 320 / max(w, h)
    small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1 else frame
    ok, jpeg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    image = "data:image/jpeg;base64," + base64.b64encode(jpeg.tobytes()).decode() if ok else None
    return {"level": "ok", "message": f"Connected: picture received ({w}×{h}).", "image": image}
