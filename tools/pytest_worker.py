"""One pytest process with private preferences; never edit the running editor's settings."""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QSettings

profile = os.environ.get("MEENIFX_TEST_PROFILE")
if profile:
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, profile)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)

import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
