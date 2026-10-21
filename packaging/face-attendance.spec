# PyInstaller build: Python and all libraries in one folder, no models (downloaded on the first start).
#   pip install . pyinstaller certifi && pyinstaller packaging/face-attendance.spec
# Output: dist/FaceAttendance/ (Windows, Linux) or dist/FaceAttendance.app (macOS).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root = Path(SPECPATH).parent
icon = None

a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root)],
    binaries=collect_dynamic_libs("onnxruntime"),
    datas=[(str(root / "attendance" / "web" / "templates"), "attendance/web/templates"),
           (str(root / "attendance" / "web" / "static"), "attendance/web/static"),
           (str(root / "config.example.yaml"), ".")]
          + collect_data_files("certifi")
          + (collect_data_files("tzdata") if sys.platform == "win32" else []),  # elsewhere the system's time zones
    hiddenimports=["attendance.__main__", "attendance.core.pipeline", "attendance.web", "certifi"],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide6", "IPython", "pytest"],
)
if sys.platform == "win32":
    # Python and OpenCV bring an older Microsoft C++ runtime than onnxruntime needs (it crashes with
    # an access violation when loading a model). Ship the newest one installed on the build computer.
    import os

    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    a.binaries = [(dest, str(system32 / Path(dest).name)
                   if Path(dest).name.lower().startswith(("msvcp140", "vcruntime140", "concrt140"))
                   and (system32 / Path(dest).name).exists() else src, kind)
                  for dest, src, kind in a.binaries]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="FaceAttendance",
    console=sys.platform.startswith("linux"),  # Windows and macOS: no console window, it runs in the background
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="FaceAttendance")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="FaceAttendance.app", bundle_identifier="com.faceattendance.app",
                 info_plist={"CFBundleDisplayName": "Face Attendance", "LSUIElement": True,
                             "LSMinimumSystemVersion": "12.0",  # the libraries are resolved for macOS 12 (build.yml)
                             "NSCameraUsageDescription": "Not used: the program reads a network camera."})
