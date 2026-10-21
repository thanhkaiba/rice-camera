"""Entry point of the packaged program (PyInstaller); see attendance/cli/launcher.py."""
import os

try:  # the packaged Python cannot rely on the computer's certificate files (macOS especially)
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

from attendance.cli.launcher import main

if __name__ == "__main__":
    main()
