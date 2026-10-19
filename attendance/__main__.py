"""Command line: python -m attendance <command> --config config.yaml"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import logging.handlers
import sys
from pathlib import Path

from .core import config as config_module


def setup_logging(cfg) -> None:
    # A Windows service writes the console log with the legacy code page; never fail on emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    log_dir = cfg.path(cfg.data_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [
        *([logging.StreamHandler()] if sys.stderr else []),  # none in the packaged Windows program
        logging.handlers.RotatingFileHandler(log_dir / "attendance.log", maxBytes=5_000_000, backupCount=5,
                                             encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m attendance")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run camera processing, notifications and the admin page")
    p = sub.add_parser("download-models", help="download the model files for a backend")
    p.add_argument("--backend", choices=["insightface", "opencv"], default=None,
                   help="default: the backend in config.yaml")
    p.add_argument("--person", choices=["yolox_tiny", "yolox_s"], nargs="?", const="yolox_tiny", default=None,
                   help="download a person detector for tracking.mode: person instead (default yolox_tiny)")
    p = sub.add_parser("enroll", help="enroll reference photos from faces/<email>/")
    p.add_argument("--faces", default="faces")
    p = sub.add_parser("evaluate", help="compare backends and choose thresholds from labelled photos")
    p.add_argument("--gallery", default="faces")
    p.add_argument("--test", default=None)
    p.add_argument("--backends", nargs="+", choices=["insightface", "opencv"], default=None,
                   help="default: the backend in config.yaml")
    p.add_argument("--from-data", action="store_true",
                   help="test on the camera's own snapshots, labelled by admin corrections, instead of folders")
    p.add_argument("--days", type=int, default=30, help="with --from-data: how many days back (default 30)")
    p = sub.add_parser("benchmark", help="run a video through both tracking modes; compare speed and results")
    p.add_argument("video")
    p.add_argument("--modes", nargs="+", choices=["face", "person"], default=["face", "person"])
    p = sub.add_parser("report", help="print the summary for a day")
    p.add_argument("--day", default=None, help="YYYY-MM-DD, default today")
    sub.add_parser("test-chat", help="send a test message to Google Chat")
    sub.add_parser("self-test", help="check that the libraries and the models load (used after building the packages)")
    p = sub.add_parser("backup", help="copy the database, settings and photos to the backup folder now")
    p.add_argument("folder", nargs="?", default=None, help="default: the folder set in Settings")
    p = sub.add_parser("diagnostics", help="write logs, settings and state (secrets removed) to a zip to send for help")
    p.add_argument("out", nargs="?", default=None, help="default: attendance-diagnostics-<computer>-<time>.zip here")
    p = sub.add_parser("restore", help="put a backup back into this install (stop the program first)")
    p.add_argument("folder")
    p.add_argument("--snapshot", default=None, help="which database copy, YYYY-MM-DD (default: the newest)")
    p = sub.add_parser("export-config", help="write config.yaml and the saved settings to a file (holds passwords)")
    p.add_argument("file")
    p = sub.add_parser("import-config", help="set this install up from an exported configuration file")
    p.add_argument("file")
    p = sub.add_parser("import-seed", help="import employees, reference photos and review samples from a seed "
                                           "folder or .zip (stop the program first, or use the People page)")
    p.add_argument("seed")
    p.add_argument("--review-only", action="store_true", help="only add review samples; keep employees and photos")
    args = parser.parse_args()

    cfg = config_module.load(args.config)
    setup_logging(cfg)

    if args.command == "run":
        from .core.pipeline import run

        run(cfg)
    elif args.command == "download-models":
        from .vision.models import download, download_person

        if args.person:
            download_person(args.person, cfg.path("models"))
        else:
            download(args.backend or cfg.recognition.backend, cfg.path("models"))
    elif args.command == "enroll":
        from .cli.enroll import enroll

        enroll(cfg, cfg.path(args.faces))
    elif args.command == "evaluate":
        if args.from_data:
            from .cli.evaluate import evaluate_data
            from .core.pipeline import App

            evaluate_data(App(cfg), args.days)
        else:
            from .cli.evaluate import evaluate

            evaluate(cfg, cfg.path(args.gallery), cfg.path(args.test) if args.test else None,
                     args.backends or [cfg.recognition.backend])
    elif args.command == "benchmark":
        from .cli.benchmark import benchmark
        from .core.pipeline import App

        benchmark(App(cfg), Path(args.video), args.modes)
    elif args.command == "backup":
        from .core.pipeline import App

        app = App(cfg)
        if args.folder:
            cfg.backup.folder = args.folder
        result = app.backup.run_now("command line")
        print(result.message)
        if not result.ok:
            raise SystemExit(1)
    elif args.command == "diagnostics":
        from .core import diagnostics, settings
        from .store import Store

        store = Store(cfg.path(cfg.data_dir) / "attendance.db")
        try:
            settings.load(cfg, store)
            out = Path(args.out or diagnostics.file_name(dt.datetime.now(cfg.tz)))
            out.write_bytes(diagnostics.build(cfg, store, Path(args.config)))
        finally:
            store.close()
        print(f"Written {out.resolve()}")
    elif args.command == "restore":
        from .core.backup import restore

        print(restore(cfg, Path(args.folder), args.snapshot))
    elif args.command in ("export-config", "import-config"):
        import json

        from .core import transfer
        from .store import Store

        store = Store(cfg.path(cfg.data_dir) / "attendance.db")
        try:
            if args.command == "export-config":
                data = transfer.export(cfg, store, Path(args.config))
                Path(args.file).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"Written to {args.file}. It holds the password and keys: keep it private.")
            else:
                try:
                    data = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
                    backup = transfer.import_(cfg, store, Path(args.config), data)
                except (OSError, ValueError) as exc:
                    raise SystemExit(f"Not imported: {exc}")
                if getattr(sys, "frozen", False):  # the running program restarts by itself to apply it
                    from .cli.updater import RESTART_FILE

                    (cfg.path(cfg.data_dir) / RESTART_FILE).touch()
                print(f"Imported; the previous configuration is saved as {backup}. "
                      "Restart the program to apply all of it (the installed program does this by itself).")
        finally:
            store.close()
    elif args.command == "import-seed":
        import tempfile

        from .cli.launcher import Instance
        from .cli.seed import find_seed, import_seed, unpack
        from .core.pipeline import App

        instance = Instance(Path(args.config).resolve().parent)
        if not instance.acquire():  # also keeps the launcher from starting the program meanwhile
            raise SystemExit("The program is running: stop it first, or import the seed on the People page.")
        source = Path(args.seed)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                seed = unpack(source, Path(tmp)) if source.is_file() else find_seed(source)
                app = App(cfg)
                try:
                    import_seed(app, seed, args.review_only)
                finally:
                    app.store.close()
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(f"Seed import failed: {exc}")
        finally:
            instance.release()
    elif args.command == "self-test":
        import numpy as np

        from .core.pipeline import App
        from .web import create_app

        app = App(cfg)
        faces = app.recognizer().detect(np.zeros((480, 640, 3), dtype=np.uint8))
        pages = create_app(app).test_client()
        assert pages.get("/").status_code in (200, 302) and pages.get("/settings").status_code == 200
        print(f"Self-test passed: models load, {len(faces)} faces in a black picture, pages render.")
    elif args.command in ("report", "test-chat"):
        from .core.pipeline import App

        app = App(cfg)
        if args.command == "report":
            day = dt.date.fromisoformat(args.day) if args.day else app.service.now().date()
            print(app.service.summary_text(app.service.report(day)))
        else:
            if not cfg.google_chat.webhook_url:
                raise SystemExit("google_chat.webhook_url is not set")
            try:
                app.chat.send_now("✅ The attendance system can post to this space.")
            except Exception as exc:
                raise SystemExit(f"Sending failed: {exc}")
            print("Sent, check Google Chat.")


if __name__ == "__main__":
    main()
