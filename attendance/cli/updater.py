"""Automatic updates for the packaged program.

New versions are published as GitHub releases (tag v1.2.3) with one archive per platform, e.g.
FaceAttendance-windows-x64.zip. The launcher checks for a newer release now and then, unpacks it
into <data folder>/versions/<version>/ and, outside camera hours, restarts into it. The installed
program stays where the installer put it and hands over to the newest unpacked version when it
starts, so updating never needs to write to the install folder or ask for permission.

The Settings page can go back to the previous version (an older unpacked one or the installed
program): that version is pinned, and the one gone back from is skipped until a newer release.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import platform
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .. import __version__

log = logging.getLogger(__name__)

REPO = "thanhkaiba/rice-camera"  # renamed from simple-go-gin; GitHub redirects the old name
STATUS_FILE = "update.json"     # in the data folder; shown on the Settings page
REQUEST_FILE = "update.request"  # written by the Settings page: check now
ROLLBACK_FILE = "rollback.request"  # written by the Settings page: {"version": "1.2.0"}, or "" for the newest again
RESTART_FILE = "restart.request"  # written after a configuration import: restart to apply it
PIN_FILE = "pinned.json"  # in <home>/versions: the program chosen by going back; run instead of the newest
BASE_FILE = "installed.json"  # in <home>/versions: version and path of the program the installer put in place
KEEP_VERSIONS = 2


def parse(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in version.lstrip("v").split("-")[0].split("."))
    except ValueError:
        return (0,)


def platform_name() -> str:
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"{system}-{arch}"


def asset_name() -> str:
    return f"FaceAttendance-{platform_name()}" + (".zip" if sys.platform == "win32" else ".tar.gz")


def exe_in(folder: Path) -> Path:
    if sys.platform == "darwin":
        return folder / "FaceAttendance.app" / "Contents" / "MacOS" / "FaceAttendance"
    return folder / "FaceAttendance" / ("FaceAttendance.exe" if sys.platform == "win32" else "FaceAttendance")


def latest_release(repo: str = REPO) -> tuple[str, str, str | None] | None:
    """(version, download URL, published checksum or None) of the highest-numbered recent release
    that has a package for this computer. GitHub's own "latest" is the release published last, which
    is the older version when two tags are built at the same time (1.1.34 finished after 1.1.35)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases?per_page=10",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "FaceAttendance"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            releases = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # no release published yet
            return None
        raise
    best = None
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        version = release.get("tag_name", "").lstrip("v")
        for asset in release.get("assets", []):
            if asset["name"] == asset_name() and (best is None or parse(version) > parse(best[0])):
                best = (version, asset["browser_download_url"], asset.get("digest"))
    return best


def installed(home: Path) -> list[tuple[str, Path]]:
    """Unpacked versions, newest first, as (version, program)."""
    found = []
    for folder in (home / "versions").glob("*"):
        if folder.is_dir() and not folder.name.endswith(".part") and exe_in(folder).exists():
            found.append((folder.name, exe_in(folder)))
    return sorted(found, key=lambda v: parse(v[0]), reverse=True)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def note_base(home: Path) -> None:
    """Remember the version of the program in the install folder, so a later version can go back to it."""
    if home / "versions" in Path(sys.executable).parents:
        return
    info = {"version": __version__, "program": sys.executable}
    path = home / "versions" / BASE_FILE
    if _read_json(path) != info:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(info), encoding="utf-8")


def programs(home: Path) -> list[tuple[str, Path]]:
    """Every version on this computer (unpacked updates and the installed program), newest first."""
    found = dict(installed(home))
    base = _read_json(home / "versions" / BASE_FILE)
    if base.get("version") and base.get("program") and Path(base["program"]).exists():
        found.setdefault(base["version"], Path(base["program"]))
    return sorted(found.items(), key=lambda v: parse(v[0]), reverse=True)


def pinned(home: Path) -> Path | None:
    """The program chosen by going back to an earlier version, while it is still there."""
    program = _read_json(home / "versions" / PIN_FILE).get("program")
    return Path(program) if program and Path(program).exists() else None


def pin(home: Path, program: Path | None) -> None:
    path = home / "versions" / PIN_FILE
    if program is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"program": str(program)}), encoding="utf-8")


# Going back works by pinning the earlier program (pinned.json) and skipping the newer release.
# Versions before this one know neither: started as "the version gone back to" they would hand over
# to the newest unpacked version straight away, which hands back to them, and so on.
PIN_SUPPORT = (1, 1, 8)


def rollback_targets(home: Path) -> list[tuple[str, Path]]:
    """Earlier versions this one can go back to, newest first: those that respect the pin."""
    return [(v, p) for v, p in programs(home)
            if PIN_SUPPORT <= parse(v) < parse(__version__) and p != Path(sys.executable)]


def newer_installed(home: Path) -> Path | None:
    """The program to hand over to: the version chosen by going back, else an unpacked version newer
    than this one."""
    chosen = pinned(home)
    if chosen is not None:
        return chosen if chosen != Path(sys.executable) else None
    versions = installed(home)
    if versions and parse(versions[0][0]) > parse(__version__) and versions[0][1] != Path(sys.executable):
        return versions[0][1]
    return None


def unpack(url: str, version: str, home: Path, digest: str | None = None) -> Path:
    """Download and unpack a release package into versions/<version>/. With `digest` (GitHub's
    "sha256:<hex>" for the asset), a download that does not match is refused."""
    versions = home / "versions"
    target, partial = versions / version, versions / (version + ".part")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    archive = versions / (version + "-" + url.rsplit("/", 1)[1])
    with urllib.request.urlopen(url, timeout=120) as resp, open(archive, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    if digest and ":" in digest:
        algorithm, expected = digest.split(":", 1)
        actual = hashlib.new(algorithm, archive.read_bytes()).hexdigest()
        if actual != expected.lower():
            archive.unlink(missing_ok=True)
            shutil.rmtree(partial, ignore_errors=True)
            raise RuntimeError(f"the download of version {version} does not match the published checksum")
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(partial)
        else:
            with tarfile.open(archive) as t:  # keeps the program's permissions and the macOS app's links
                t.extractall(partial, filter="tar") if hasattr(tarfile, "data_filter") else t.extractall(partial)
    finally:
        archive.unlink(missing_ok=True)
    if not exe_in(partial).exists():
        shutil.rmtree(partial, ignore_errors=True)
        raise RuntimeError(f"{url} does not contain {exe_in(Path('.'))}")
    shutil.rmtree(target, ignore_errors=True)
    partial.rename(target)
    return exe_in(target)


def remove_old(home: Path, running: Path) -> None:
    keep = pinned(home)
    for version, exe in installed(home)[KEEP_VERSIONS:]:
        if exe != running and exe != keep:
            shutil.rmtree(home / "versions" / version, ignore_errors=True)


def write_status(data: Path, **fields) -> None:
    path = data / STATUS_FILE
    status = read_status(data)
    status.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status), encoding="utf-8")


def read_status(data: Path) -> dict:
    try:
        return json.loads((data / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_settings(config: Path):
    """config.yaml with the values saved on the Settings page, read fresh each time."""
    from ..core import config as config_module, settings
    from ..store import Store

    cfg = config_module.load(config)
    store = Store(cfg.path(cfg.data_dir) / "attendance.db")
    try:
        settings.load(cfg, store)
    finally:
        store.close()
    return cfg


def camera_hours(cfg) -> bool:
    """True during the camera window on a work day: no restarts then."""
    now = dt.datetime.now(cfg.tz)
    start, _, end = cfg.schedule.clocks()
    return now.weekday() in cfg.schedule.work_days and start <= now.time() < end


class Updater:
    """Runs in the launcher, next to the attendance program. `ready` is set to the new program once
    it is unpacked and it is a good moment to restart."""

    def __init__(self, home: Path, config: Path, repo: str = REPO):
        self.home, self.config, self.repo = home, config, repo
        cfg = load_settings(config)
        self.data = cfg.path(cfg.data_dir)
        write_status(self.data, state="")  # a restart after an update starts here
        (self.data / RESTART_FILE).unlink(missing_ok=True)  # this start already reads the new configuration
        self.ready: Path | None = None
        self.waiting: Path | None = None  # unpacked, waiting for the camera hours to end

    def check(self) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        try:
            found = latest_release(self.repo)
        except Exception as exc:
            write_status(self.data, checked_at=now, error=f"Could not check for updates: {exc}")
            return
        if not found or parse(found[0]) <= parse(__version__):
            write_status(self.data, checked_at=now, latest=found[0] if found else __version__, error="", state="")
            return
        version, url, digest = (*found, None)[:3]
        skipped = read_status(self.data).get("skip")
        if skipped and parse(version) <= parse(skipped):  # gone back from it: wait for a newer release
            write_status(self.data, checked_at=now, latest=version, error="",
                         state=f"Version {version} is skipped because you went back from it")
            return
        log.info("Downloading version %s", version)
        write_status(self.data, checked_at=now, latest=version, error="", state=f"Downloading version {version}")
        try:
            self.waiting = unpack(url, version, self.home, digest)
        except Exception as exc:
            write_status(self.data, error=f"Could not download version {version}: {exc}", state="")
            return
        if skipped:  # a release newer than the one gone back from: back to installing updates as usual
            pin(self.home, None)
            write_status(self.data, skip="", rolled_back="")
        write_status(self.data, state=f"Version {version} is ready; it starts after camera hours")

    def requests(self) -> None:
        """Go back to an earlier version, forward to the newest again, or restart, as asked on the
        Settings page. Sets `ready` to the program to restart into."""
        rollback, restart = self.data / ROLLBACK_FILE, self.data / RESTART_FILE
        if rollback.exists():
            wanted = _read_json(rollback).get("version", "")
            rollback.unlink(missing_ok=True)
            self.ready = self.go_back(wanted) if wanted else self.go_forward()
        elif restart.exists():
            restart.unlink(missing_ok=True)
            self.ready = Path(sys.executable)

    def go_back(self, version: str) -> Path | None:
        program = next((p for v, p in rollback_targets(self.home) if v == version), None)
        if program is None:
            write_status(self.data, error=f"Version {version} is no longer on this computer")
            return None
        log.info("Going back from version %s to %s", __version__, version)
        pin(self.home, program)
        # The version gone back from is not installed again; a newer release is.
        write_status(self.data, skip=__version__, error="", state="",
                     rolled_back=f"Went back from version {__version__} to {version}")
        return program

    def go_forward(self) -> Path | None:
        pin(self.home, None)
        write_status(self.data, skip="", rolled_back="", error="")
        newest = programs(self.home)
        if newest and newest[0][1] != Path(sys.executable) and parse(newest[0][0]) > parse(__version__):
            log.info("Going forward from version %s to %s", __version__, newest[0][0])
            return newest[0][1]
        return None

    def loop(self, first_check_s: float = 120, poll_s: float = 30) -> None:
        next_check = time.time() + first_check_s
        request = self.data / REQUEST_FILE
        while self.ready is None:
            time.sleep(poll_s)
            try:
                self.requests()
                if self.ready is not None:
                    break
                cfg = load_settings(self.config)
                asked = request.exists()
                if asked:
                    request.unlink(missing_ok=True)
                if self.waiting is None and (asked or (cfg.updates.enabled and time.time() >= next_check)):
                    next_check = time.time() + max(cfg.updates.check_hours, 0.25) * 3600
                    self.check()
                if self.waiting is not None and (asked or not camera_hours(cfg)):
                    self.ready = self.waiting
            except Exception:
                log.exception("Update check failed")
