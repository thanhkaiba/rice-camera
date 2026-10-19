"""Start-up for the packaged program (Windows installer, macOS app, Linux folder).

Double-clicking the program: on the first start it writes config.yaml into the data folder,
registers itself to start at login and opens the admin page, where the setup guide takes over.
The face models are downloaded by the running program (see pipeline.fetch_models), so the
package carries Python and every library but no models.

It then keeps the attendance program running, restarting it if it stops, until `stop` is used.
Started again while already running, it only opens the admin page.

    FaceAttendance                 start (or open the page if running)
    FaceAttendance --background    start without opening the page (used at login)
    FaceAttendance stop            stop it
    FaceAttendance uninstall       stop it and do not start it at login any more (then delete the program)
    FaceAttendance <command> ...   any `python -m attendance` command, e.g. download-models, report
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "FaceAttendance"
DEFAULT_PORT = 8000
RESTART_AFTER_S = 10
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no console window for started programs
AUTOSTART_ID = "com.faceattendance.app"
# Windows time zone names (registry) for the offices this is used in; anything else falls back to the UTC offset.
WINDOWS_ZONES = {"SE Asia Standard Time": "Asia/Ho_Chi_Minh", "Singapore Standard Time": "Asia/Singapore",
                 "China Standard Time": "Asia/Shanghai", "Tokyo Standard Time": "Asia/Tokyo",
                 "Korea Standard Time": "Asia/Seoul", "UTC": "UTC"}


def frozen() -> bool:
    return getattr(sys, "frozen", False)


def translocated() -> bool:
    """macOS runs an app opened straight from Downloads (still quarantined) from a temporary copy
    under /private/var/folders/.../AppTranslocation. A login entry pointing there would break, so
    the app must be moved to Applications first; the admin page says so."""
    return sys.platform == "darwin" and "/AppTranslocation/" in sys.executable


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return True  # os.kill(pid, 0) would terminate the process on Windows; the lock handles it
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def app_home() -> Path:
    """Data folder: config.yaml, models, database, photos. Kept when the program is updated or removed."""
    if os.environ.get("FACE_ATTENDANCE_HOME"):
        return Path(os.environ["FACE_ATTENDANCE_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "face-attendance"


def program_command() -> list[str]:
    """How to start this program again (for autostart and the restart loop)."""
    if frozen():
        return [sys.executable]
    return [sys.executable, "-m", "attendance.launcher"]


def attendance_command(config: Path, *args: str) -> list[str]:
    if frozen():
        return [sys.executable, "--config", str(config), *args]
    return [sys.executable, "-m", "attendance", "--config", str(config), *args]


def local_timezone() -> str:
    tz = os.environ.get("TZ", "")
    if "/" in tz:
        return tz.lstrip(":")
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
                name = winreg.QueryValueEx(key, "TimeZoneKeyName")[0]
            if name in WINDOWS_ZONES:
                return WINDOWS_ZONES[name]
        except OSError:
            pass
    else:
        try:  # /etc/localtime -> /usr/share/zoneinfo/Asia/Ho_Chi_Minh (Linux and macOS)
            target = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    offset_h = -time.timezone // 3600
    return {7: "Asia/Ho_Chi_Minh", 8: "Asia/Singapore", 9: "Asia/Tokyo", 0: "UTC"}.get(offset_h, "Asia/Ho_Chi_Minh")


def write_config(home: Path, port: int = DEFAULT_PORT) -> Path:
    """config.yaml for a new install, like the script installers write. Returns its path."""
    from attendance.web.setup import lan_address

    config = home / "config.yaml"
    if config.exists():
        return config
    home.mkdir(parents=True, exist_ok=True)
    port = next((p for p in range(port, port + 20) if not answering(f"http://localhost:{p}")), port)
    config.write_text(
        "# Created on the first start. Most settings are on the admin page (Settings); see\n"
        "# config.example.yaml in the program folder for everything else. Restart after editing.\n"
        f"timezone: {local_timezone()}\n"
        "data_dir: data\n"
        "employees_file: employees.csv\n"
        "recognition:\n"
        "  backend: insightface   # or opencv (free for commercial use, less accurate)\n"
        "web:\n"
        f"  port: {port}\n"
        f'  public_url: "{lan_address(port)}"\n', encoding="utf-8")
    return config


def page_url(config: Path) -> str:
    from attendance.core import config as config_module

    return f"http://localhost:{config_module.load(config).web.port}"


def answering(url: str) -> bool:
    host, port = url.split("//", 1)[1].split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False


# ---- start at login -------------------------------------------------------------------------

def autostart_path() -> Path | None:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{AUTOSTART_ID}.plist"
    if sys.platform.startswith("linux"):
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "face-attendance.desktop"
    return None


def enable_autostart() -> None:
    cmd = program_command() + ["--background"]
    if sys.platform == "win32":
        import winreg

        line = subprocess.list2cmdline(cmd)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, line)
        return
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        args = "".join(f"<string>{a}</string>" for a in cmd)
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0"><dict><key>Label</key><string>{AUTOSTART_ID}</string>'
            f'<key>ProgramArguments</key><array>{args}</array>'
            '<key>RunAtLoad</key><true/></dict></plist>\n', encoding="utf-8")
    else:
        path.write_text("[Desktop Entry]\nType=Application\nName=Face attendance\n"
                        f"Exec={' '.join(_quote(a) for a in cmd)}\nX-GNOME-Autostart-enabled=true\n",
                        encoding="utf-8")


def disable_autostart() -> None:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, APP_NAME)
        except OSError:
            pass
        return
    path = autostart_path()
    if path and path.exists():
        path.unlink()


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


# ---- keep it running ------------------------------------------------------------------------

class Instance:
    """Only one copy runs per data folder; the lock is released by the OS if the process dies."""

    def __init__(self, home: Path):
        self.pid_file = home / "running.pid"
        self._file = open(home / "running.lock", "a+")
        self.clear_stale()

    def clear_stale(self) -> None:
        """A pid file left by a copy that died (power cut, kill) must not make `stop` or a check
        believe it still runs. The lock itself is released by the OS when a process ends."""
        try:
            pids = [int(p) for p in self.pid_file.read_text().split()]
        except (OSError, ValueError):
            return
        if pids and not any(pid_alive(p) for p in pids):
            self.pid_file.unlink(missing_ok=True)

    def acquire(self, wait_s: float = 0) -> bool:
        deadline = time.time() + wait_s
        while not self._try_lock():
            if time.time() >= deadline:
                return False
            time.sleep(1)
        return True

    def release(self) -> None:
        self._file.close()

    def _try_lock(self) -> bool:
        try:
            if sys.platform == "win32":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def write_pids(self, *pids: int) -> None:
        self.pid_file.write_text(" ".join(str(p) for p in pids))


def stop(home: Path) -> bool:
    pid_file = home / "running.pid"
    if not pid_file.exists():
        return False
    stopped = False
    for pid in pid_file.read_text().split():  # the restart loop first, so it does not start the program again
        try:
            os.kill(int(pid), signal.SIGTERM)
            stopped = True
        except (OSError, ValueError):
            pass
    pid_file.unlink(missing_ok=True)
    return stopped


def supervise(config: Path, instance: Instance, unclean: bool = False) -> None:
    """Keep the attendance program running; when an update is ready, restart into the new version.
    Google Chat hears when it keeps crashing, and (`unclean`) when the last run was cut off."""
    from attendance.cli.updater import Updater, remove_old
    from attendance.cli.watchdog import CrashWatch, chat_poster

    home = config.parent
    updates = Updater(home, config) if frozen() else None
    if updates:
        remove_old(home, Path(sys.executable))
        threading.Thread(target=updates.loop, name="updates", daemon=True).start()
    if sys.platform == "darwin":  # keep the Mac from sleeping while this runs
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
    log_dir = home / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    post = chat_poster(config)
    watch = CrashWatch(post)
    if unclean:
        post(f"ℹ️ The attendance program on {watch.host} started again after it was stopped without shutting "
             "down (power cut, forced restart or crash).")
    child: subprocess.Popen | None = None

    def on_terminate(signum, frame):
        # Asked to stop (`stop`, logout, shutdown): a clean stop leaves no pid file, so the next
        # start can tell it apart from a power cut.
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
        instance.pid_file.unlink(missing_ok=True)
        raise SystemExit(0)

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_terminate)
    with open(log_dir / "service.log", "a", encoding="utf-8") as log:  # crashes the program could not log
        def note(text: str) -> None:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
            log.flush()

        while True:
            child = subprocess.Popen(attendance_command(config, "run"), cwd=home, creationflags=NO_WINDOW,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            instance.write_pids(os.getpid(), child.pid)
            watch.started()
            while child.poll() is None and not (updates and updates.ready):
                time.sleep(1)
                watch.tick()
            if updates and updates.ready:
                note(f"Restarting into the update: {updates.ready}")
                child.terminate()
                child.wait()
                instance.pid_file.unlink(missing_ok=True)  # a planned stop, not a crash
                instance.release()  # the new version takes the lock
                subprocess.Popen([str(updates.ready), "--background"], cwd=home, creationflags=NO_WINDOW,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return
            instance.write_pids(os.getpid())
            note(f"The attendance program stopped (exit code {child.returncode}); "
                 f"starting it again in {RESTART_AFTER_S} s")
            watch.exited(child.returncode)
            time.sleep(RESTART_AFTER_S)


def open_when_ready(url: str, wait_s: float = 120) -> None:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if answering(url):
            webbrowser.open(url)
            return
        time.sleep(1)


def hand_over(home: Path, argv: list[str]) -> bool:
    """Start the newest downloaded version instead of this one. True if it did."""
    from attendance.cli.updater import newer_installed

    newer = newer_installed(home) if frozen() else None
    if newer is None:
        return False
    if argv and argv[0] not in ("--background", "--detached"):  # a command: run it there and wait for it
        sys.exit(subprocess.call([str(newer), *argv], creationflags=NO_WINDOW))
    subprocess.Popen([str(newer), *argv], creationflags=NO_WINDOW, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def install_source(uv: str, source: Path) -> None:
    """Let uv select wheels for this Python and machine before installing the program."""
    if frozen() or sys.prefix == sys.base_prefix:
        raise SystemExit("Source installation requires an unbundled Python virtual environment.")
    command = [uv, "pip", "install", "--python", sys.executable]
    subprocess.run([*command, "--upgrade", "--only-binary", ":all:",
                    "-r", str(source / "requirements.txt")], check=True)
    # A fresh process checks native-library loading before the program is replaced.
    subprocess.run([sys.executable, "-c",
                    "import cv2, onnxruntime, numpy, flask, yaml\n"
                    "if not cv2.videoio_registry.hasBackend(cv2.CAP_FFMPEG):\n"
                    "    raise SystemExit('OpenCV has no FFmpeg backend for RTSP.')\n"
                    "print(f'Libraries ready: OpenCV {cv2.__version__}, '"
                    "f'ONNX Runtime {onnxruntime.__version__}, NumPy {numpy.__version__}; FFmpeg available.')"],
                   check=True)
    subprocess.run([*command, "--no-deps", "--reinstall-package", "face-attendance", str(source)], check=True)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--install":
        if len(argv) != 3:
            raise SystemExit("Usage: launcher.py --install <uv> <source-folder>")
        try:
            install_source(argv[1], Path(argv[2]))
        except subprocess.CalledProcessError as exc:
            raise SystemExit(exc.returncode) from exc
        return
    home = app_home()
    if hand_over(home, argv):
        return
    if sys.platform == "darwin" and frozen() and not argv:
        # Opened from Finder: macOS expects the app to answer its events, which this one (no window) never
        # does, so it shows as "Not Responding" and ignores a second double-click. Run as a separate process.
        subprocess.Popen([sys.executable, "--detached"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if argv and argv[0] == "stop":
        print("Stopped." if stop(home) else "It was not running.")
        return
    if argv and argv[0] in ("remove-autostart", "uninstall"):  # used by the uninstallers
        if argv[0] == "uninstall":
            stop(home)
        disable_autostart()
        return
    if argv and argv[0] not in ("--background", "--detached"):
        # A command for the attendance program itself (run, download-models, report, ...).
        if "--config" not in argv:
            argv = ["--config", str(write_config(home)), *argv]
        sys.argv = [sys.argv[0], *argv]
        from attendance.__main__ import main as attendance_main

        attendance_main()
        return

    background = "--background" in argv
    first_start = not (home / "config.yaml").exists()
    config = write_config(home)
    url = page_url(config)
    from attendance.cli.watchdog import unclean_stop

    unclean = unclean_stop(home)  # before Instance clears the pid file it left
    instance = Instance(home)
    if not instance.acquire(wait_s=30 if background else 0):  # after an update the old version is still stopping
        if not background:
            webbrowser.open(url)
        return
    # The login entry starts the installed program (which hands over to updates), never an update itself;
    # nor with a custom data folder, which the entry would not carry.
    if (frozen() and not os.environ.get("FACE_ATTENDANCE_HOME") and not translocated()
            and home / "versions" not in Path(sys.executable).parents):
        enable_autostart()
    if not background:
        threading.Thread(target=open_when_ready, args=(url + ("/setup" if first_start else ""),), daemon=True).start()
    supervise(config, instance, unclean)


if __name__ == "__main__":
    main()
