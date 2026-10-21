#!/bin/bash
# Camera attendance: install or update on this Mac. Double-click this file in Finder.
# (If macOS refuses to open it: right-click -> Open, or run `bash Install.command` in Terminal.)
#
# Installs into ~/Library/Application Support/FaceAttendance, with its own Python (no admin
# rights needed), downloads the models, starts the program now and at every login, and opens
# the Setup page. Running it again from a newer copy updates the program; settings, records and
# photos are kept.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_HOME="${FACE_ATTENDANCE_HOME:-$HOME/Library/Application Support/FaceAttendance}"
LABEL="com.faceattendance.camera"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${FACE_ATTENDANCE_PORT:-8000}"
PY="$APP_HOME/venv/bin/python"

step() { printf "\n\033[1m== %s\033[0m\n" "$1"; }
fail() { printf "\n\033[31mInstallation failed: %s\033[0m\n" "$1"; read -r -p "Press Enter to close." _ || true; exit 1; }
trap 'fail "see the messages above (is the internet connection working?)"' ERR

mkdir -p "$APP_HOME/data"
cd "$APP_HOME"
echo "Camera attendance: installing from $SRC"
echo "into $APP_HOME"

step "1/6 Python"
UV="$APP_HOME/tools/uv"
if [ ! -x "$UV" ] && [ ! -x "$APP_HOME/tools/bin/uv" ]; then
  echo "Downloading uv (it installs Python just for this program)..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$APP_HOME/tools" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
fi
[ -x "$UV" ] || UV="$APP_HOME/tools/bin/uv"
[ -x "$UV" ] || fail "could not install uv"
if [ ! -x "$PY" ]; then
  "$UV" venv --python 3.12 "$APP_HOME/venv"
fi

step "2/6 Program and libraries"
"$PY" "$SRC/attendance/cli/launcher.py" --install "$UV" "$SRC"

step "3/6 Settings file"
if [ -f config.yaml ]; then
  echo "Keeping the existing config.yaml"
else
  TZ_NAME="$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')"
  [ -n "$TZ_NAME" ] || TZ_NAME="Asia/Ho_Chi_Minh"
  IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)"
  cat > config.yaml <<EOF
# Created by the installer. Most settings are on the admin page (Settings); see config.example.yaml
# in the program folder for everything else. Restart the program after editing this file.
timezone: $TZ_NAME
data_dir: data
employees_file: employees.csv
recognition:
  backend: insightface   # or opencv (free for commercial use, less accurate); both are downloaded
web:
  port: $PORT
  public_url: "http://$IP:$PORT"
EOF
  echo "Created config.yaml (time zone $TZ_NAME)"
fi

step "4/6 Recognition models (about 300 MB the first time)"
need() { for f in "$@"; do [ -s "models/$f" ] || return 0; done; return 1; }
if need det_10g.onnx w600k_r50.onnx; then "$PY" -m attendance --config config.yaml download-models --backend insightface; fi
if need face_detection_yunet_2023mar.onnx face_recognition_sface_2021dec.onnx; then
  "$PY" -m attendance --config config.yaml download-models --backend opencv
fi
if need yolox_tiny.onnx; then "$PY" -m attendance --config config.yaml download-models --person; fi
if need u2netp.onnx; then "$PY" -m attendance --config config.yaml download-models --cutout; fi
echo "Models are in $APP_HOME/models"

step "5/6 Start now and at every login"
launchctl unload "$PLIST" 2>/dev/null || true
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$APP_HOME</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$PY</string><string>-m</string><string>attendance</string>
    <string>--config</string><string>$APP_HOME/config.yaml</string><string>run</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$APP_HOME/data/service.log</string>
  <key>StandardErrorPath</key><string>$APP_HOME/data/service.log</string>
</dict></plist>
EOF
launchctl load -w "$PLIST"
echo "Started. It also starts by itself after every login (caffeinate keeps the Mac awake while it runs)."

step "6/6 Opening the admin page"
[ -d "$HOME/Desktop" ] && cat > "$HOME/Desktop/Camera attendance.webloc" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>URL</key><string>http://localhost:$PORT/</string></dict></plist>
EOF
for _ in $(seq 1 90); do
  if curl -s -o /dev/null "http://localhost:$PORT/setup"; then break; fi
  sleep 1
done
open "http://localhost:$PORT/setup" 2>/dev/null || true

trap - ERR
printf "\n\033[32mDone.\033[0m The admin page is http://localhost:%s (shortcut on the Desktop).\n" "$PORT"
echo "From other computers: see 'Address of this page' on the Setup page."
echo "If macOS asks whether Python may accept incoming network connections, click Allow."
echo "Program log: $APP_HOME/data/attendance.log"
read -r -p "Press Enter to close this window." _ || true
