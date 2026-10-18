# Installing and keeping it running

## Installer (recommended)

Download the installer for your computer from the newest [release](https://github.com/thanhkaiba/rice-camera/releases/latest). It contains Python and every library; only the face models (about 300 MB) are downloaded, by the program itself on its first start, with the progress shown on the page. No admin rights needed.

| | Windows | macOS | Linux |
|---|---|---|---|
| Download | `FaceAttendance-Setup-<version>.exe` | `FaceAttendance-<version>-macos-arm64.dmg` (Apple chip) or `-macos-x64.dmg` (Intel) | `FaceAttendance-<version>-linux-x64-installer.tar.gz` |
| Install | run it (SmartScreen: *More info → Run anyway*) | open the .dmg, drag *FaceAttendance* to *Applications*, then right-click it there → *Open* the first time. Needs macOS 12 or newer. Always open it from Applications: opened from Downloads, macOS runs a temporary copy that cannot start at login (the page then says so) | unpack, run `./install.sh` |
| Settings, data, models | `%LOCALAPPDATA%\FaceAttendance` | `~/Library/Application Support/FaceAttendance` | `~/.local/share/face-attendance` |
| Remove | *Settings → Apps → Face Attendance* | run `/Applications/FaceAttendance.app/Contents/MacOS/FaceAttendance uninstall` in Terminal, then delete the app | `./uninstall.sh` |

The program starts at login, restarts itself if it stops, and opens the **Setup** page the first time. Opening it again just opens the page.

**Automatic updates are off by default.** Tick the box in *Settings → Access → Updates* and every 6 hours the program looks for a newer release, downloads it into the data folder and, outside camera hours, restarts into it. That page shows the version and has *Check now*. Data and settings are kept. Coming from the install script below: install over it; it uses the same data folder and stops the old copy.

## Install script (from the source folder)

No commands needed, and no admin rights. The script gets its own Python, the libraries and the models, starts the program now and at every login, puts a shortcut on the Desktop and opens the **Setup** page.

The launcher selects prebuilt libraries compatible with this computer's OS, CPU and Python. It checks that ONNX Runtime and OpenCV's FFmpeg backend load before installing the program and starting it. If no compatible wheels are available, or the libraries fail to load, installation stops with an error.

1. Download this repository (on GitHub: *Code → Download ZIP*) and unzip it.
2. Double-click the installer in the unzipped folder:
   - **macOS:** `Install.command`. If macOS says it cannot be opened, right-click it → *Open* → *Open*. When asked whether Python may accept incoming network connections, click *Allow*.
   - **Windows:** `Install.bat`. If SmartScreen warns, click *More info* → *Run anyway*. When Windows Firewall asks about Python, allow it on *private networks*.

| | macOS | Windows |
|---|---|---|
| Program, settings and data | `~/Library/Application Support/FaceAttendance` | `%LOCALAPPDATA%\FaceAttendance` |
| Starts automatically | at login (LaunchAgent; restarted if it stops; keeps the Mac awake) | at login (Startup folder; restarted if it stops) |
| Update | run the new version's `Install.command` (or `Update.command` in a git checkout) | run the new version's `Install.bat` |
| Remove | `Uninstall.command` | `Uninstall.bat` |

Updating keeps settings, attendance records and photos; *Uninstall* asks before deleting them. The program log is `data/attendance.log` in the folder above.

## Manual install

Python 3.10 or newer. On macOS 12 or older use Python 3.12 (not 3.13).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml  # Windows: copy
python -m attendance download-models
python -m attendance run
```

Allow Python on private networks when the firewall asks, so the page can be opened from other computers at `http://<computer IP>:8000`. On Linux with an NVIDIA GPU, install `onnxruntime-gpu` instead of `onnxruntime` and set `use_gpu: true`.

## First-time setup

For the prepared employee/photo/review seed, follow [Nhập employee và ảnh sau khi pull code](seed-import.md). It covers transferring the ignored `seed/` directory, importing employees by email, and updating an existing system.

People are identified by a unique email and a display name. Add them on People or import a CSV with `email,name` columns; emails are trimmed and lowercased. Names can repeat, and editing a name or email keeps the person's photos and attendance history. For older installations, the configured employee CSV attaches emails to existing records only when the name matches exactly one legacy person and one CSV row. Ambiguous matches stop the import; set those emails individually on People first. Invalid or old `id,name` CSV files are skipped at startup, so People stays available. After a successful import, restarts keep your edits; import again on People to apply later CSV changes.

The Setup page walks through it: camera address (for Ezviz, `rtsp://admin:<VERIFICATION_CODE>@<CAMERA_IP>:554/Streaming/Channels/101`; the code is the 6 capital letters on the camera label; give the camera a fixed IP in the router), working hours, the Google Chat webhook (space name → *Apps & integrations* → *Webhooks* → *Add webhook*), a password, the address other computers use, and the employees.

**@mentions in Google Chat** only work with each person's Google Chat user ID: in the Google Admin console under *Directory → Users*, open the person; the number at the end of the page address is the ID. Enter it on the person's page (*Test mention* checks it) or in a `chat_user` column of the CSV import. People without an ID appear as plain "@Name" without a notification.

## Keep it running

**Clock.** The times in the records and on the evidence pictures come from this computer's clock; leave automatic time synchronisation on (the default on macOS and Windows).

**Computer settings.**
- **Windows:** Power → Sleep → *Never*. Set Windows Update *active hours* to cover the morning. In the BIOS, enable *Restore on AC Power Loss*.
- **macOS:** System Settings → *Energy* (or *Battery → Options*): enable *Prevent automatic sleeping* and *Start up automatically after a power failure*. Turn off automatic macOS updates during the morning. Enable automatic login for the user.
- **All computers:** a UPS for the computer, router and camera is recommended.

**Start automatically (manual install only;** the installer and the install script already do this).

*Windows*, with [NSSM](https://nssm.cc):
```bat
nssm install Attendance "C:\attendance\.venv\Scripts\python.exe" "-m attendance run"
nssm set Attendance AppDirectory "C:\attendance"
nssm start Attendance
```

*macOS*: save as `~/Library/LaunchAgents/com.company.attendance.plist`, adjusting the paths, then `launchctl load ~/Library/LaunchAgents/com.company.attendance.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.company.attendance</string>
  <key>WorkingDirectory</key><string>/Users/admin/attendance</string>
  <key>ProgramArguments</key><array>
    <string>/Users/admin/attendance/.venv/bin/python</string><string>-m</string><string>attendance</string><string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

*Linux*, `/etc/systemd/system/attendance.service`, then `sudo systemctl enable --now attendance`:
```ini
[Unit]
Description=Camera attendance
After=network-online.target

[Service]
WorkingDirectory=/opt/attendance
ExecStart=/opt/attendance/.venv/bin/python -m attendance run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**If Google Chat is slow or unreachable**, messages wait in a queue on disk and are delivered later, in order; messages older than a day are dropped.

**Alert when the computer stops.** Create a free check on [Healthchecks.io](https://healthchecks.io) with a 1-minute period and 5-minute grace, and enter its ping URL in Settings.

**Remote access.** The admin page only works inside the office network. For remote access, install [Tailscale](https://tailscale.com) on this computer and the admin's devices. Never open a port to the internet.

## Backup and moving to another computer

**Backup.** In *Settings → Access & privacy → Backup*, choose a folder on an external drive, a network share, or a folder that Google Drive / OneDrive / Dropbox keeps in the cloud. Once a day (12:00 by default, after camera hours) the program writes there:

| | |
|---|---|
| `attendance-backup/db/attendance-<date>.db` | a consistent copy of the database (records, sightings and their hashes, settings, learned faces), one per day, the newest 30 kept |
| `attendance-backup/config.yaml` | the settings file |
| `attendance-backup/data/faces/` | reference photos, mirrored |
| `attendance-backup/data/crops/` | camera pictures (the evidence), mirrored; untick *Include camera pictures* to leave them out |

*Back up now* runs it at once; the page shows the last result. The Today page warns when a backup failed or is more than two days old, and a warning is posted to Google Chat once a day while it fails. From a terminal: `python -m attendance --config config.yaml backup [folder]`.

**Moving to another computer, or after a disk failure.** Install the program on the new computer and open it once (that creates the data folder), stop it, then run:

- installer: `FaceAttendance stop`, then `FaceAttendance restore "<backup folder>"` (macOS: `/Applications/FaceAttendance.app/Contents/MacOS/FaceAttendance ...`)
- script or source install: `python -m attendance --config config.yaml restore "<backup folder>"`

It puts back the newest database copy (`--snapshot 2026-10-01` picks an older one), the photos, and `config.yaml` when the new computer has none. Start the program again and check the camera address and *Address of this page* in Settings: the new computer may have another IP address.

**By hand** works too: the data folder (see the table above) is self-contained, so copying it to the same place on the new computer is a complete move.

## Publishing a release

Change `__version__` in `attendance/__init__.py` if you like, then push a tag (`git tag v1.2.0 && git push origin v1.2.0`). GitHub Actions (`.github/workflows/build.yml`) builds and tests the packages on Windows, macOS and Linux and publishes the release. Without git at hand: on GitHub, *Actions → Build packages → Run workflow*, pick the branch and type the version in *release*; that builds, creates the tag and publishes. Either way, installed copies with automatic updates on pick it up within hours. Build one locally with `pip install . pyinstaller certifi && pyinstaller packaging/face-attendance.spec`.
