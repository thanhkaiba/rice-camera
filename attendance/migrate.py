"""Copy the office Mac's database and photos to the server, once, while the server is in shadow mode.

    python -m attendance.migrate --data <folder with attendance.db> --url https://... --password-file <file>

config.yaml is taken from the folder above --data; what only makes sense on the Mac is dropped by the server.

Without --send it only says what it would copy. Photos go first and only what the server does not have yet,
so it can be run again after a failure; the database goes last. Do this before the Mac starts mirroring events:
an event the server applies during the copy would be replaced along with the old database.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import gzip
import json
import os
import sqlite3
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FOLDERS = ("crops", "faces", "late", "review-seed")
PHOTOS = (".jpg", ".jpeg")
COUNTED = ("employees", "embeddings", "checkins", "sightings", "unknowns", "audit", "notices")
ATTEMPTS = 5
WORKERS = 6


class Refused(Exception):
    pass


class Site:
    def __init__(self, url: str, password: str, user: str = "admin"):
        self.url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        cafile = None
        try:
            import certifi

            cafile = certifi.where()
        except ImportError:
            pass
        self.tls = ssl.create_default_context(cafile=cafile)

    def call(self, method: str, path: str, body: bytes | None = None, timeout: float = 60.0) -> bytes:
        """Tries again when the network or the server fails; a refusal (4xx) is final."""
        for attempt in range(ATTEMPTS):
            request = urllib.request.Request(self.url + path, data=body, method=method, headers={"Authorization": self.auth})
            try:
                with urllib.request.urlopen(request, timeout=timeout, context=self.tls) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise Refused(f"{method} {path}: HTTP {exc.code}") from None
                failure = exc
            except OSError as exc:
                failure = exc
            time.sleep(2 ** attempt)
        raise ConnectionError(f"{method} {path}: {failure}")


def local_photos(data_dir: Path) -> dict[str, Path]:
    found = {}
    for folder in FOLDERS:
        for path in (data_dir / folder).rglob("*"):
            if path.is_file() and path.suffix.lower() in PHOTOS and not path.name.startswith("."):
                found[path.relative_to(data_dir).as_posix()] = path
    return found


def missing(site: Site, photos: dict[str, Path]) -> list[str]:
    remote = json.loads(site.call("GET", "/api/edge/import/manifest", timeout=120.0))
    return [rel for rel, path in photos.items() if remote.get(rel) != path.stat().st_size]


def send_photos(site: Site, photos: dict[str, Path], todo: list[str]) -> list[str]:
    """Returns what could not be sent."""
    def send(rel: str) -> str | None:
        try:
            site.call("PUT", "/api/edge/import/photo/" + urllib.parse.quote(rel), photos[rel].read_bytes())
            return None
        except (Refused, ConnectionError, OSError) as exc:
            print(f"  not sent: {exc}", file=sys.stderr)
            return rel

    failed = []
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        for done, result in enumerate(pool.map(send, todo), 1):
            if result:
                failed.append(result)
            if done % 500 == 0 or done == len(todo):
                print(f"  photos {done}/{len(todo)}, {len(failed)} failed", flush=True)
    return failed


def snapshot(database: Path, target: Path) -> dict[str, int]:
    """A consistent copy, safe while the attendance app is running; returns its row counts."""
    source = sqlite3.connect(f"file:{urllib.parse.quote(str(database))}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
        return {table: copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in COUNTED}
    finally:
        copy.close()
        source.close()


def wait_until_up(site: Site, seconds: float = 90.0) -> None:
    time.sleep(3)
    deadline = time.monotonic() + seconds
    while True:
        try:
            site.call("GET", "/healthz", timeout=10.0)
            return
        except (Refused, ConnectionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


def run(data_dir: Path, site: Site, send: bool) -> int:
    photos = local_photos(data_dir)
    todo = missing(site, photos)
    size = sum(photos[rel].stat().st_size for rel in todo)
    print(f"{len(photos)} photos here, {len(todo)} to send ({size >> 20} MB)")
    if not send:
        print("Nothing was sent. Add --send to copy.")
        return 0
    config = data_dir.parent / "config.yaml"
    if config.exists():
        print("Sending config.yaml:", json.loads(site.call("PUT", "/api/edge/import/config", config.read_bytes())), flush=True)
    send_photos(site, photos, todo)
    left = missing(site, photos)
    if left:
        print(f"STOPPED: {len(left)} photos did not arrive, the database was not sent. Run again.", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="checkin-migrate.") as folder:
        os.chmod(folder, 0o700)
        copy = Path(folder) / "attendance.db"
        counts = snapshot(data_dir / "attendance.db", copy)
        print("Sending the database:", counts, flush=True)
        received = json.loads(site.call("PUT", "/api/edge/import/database", gzip.compress(copy.read_bytes()), timeout=180.0))
    if received != counts:
        print(f"STOPPED: the server read different counts: {received}", file=sys.stderr)
        return 1
    wait_until_up(site)
    live = json.loads(site.call("GET", "/api/edge/import/status"))
    if live != counts:
        print(f"FAILED: after the restart the server has {live}, expected {counts}", file=sys.stderr)
        return 1
    print("DONE: the server has every photo and the same row counts:", live)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--password-file", required=True, type=Path)
    parser.add_argument("--send", action="store_true", help="really copy; without it, only report")
    args = parser.parse_args()
    return run(args.data, Site(args.url, args.password_file.read_text().strip()), args.send)


if __name__ == "__main__":
    sys.exit(main())
