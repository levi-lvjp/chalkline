"""Loaded by Hermes' authenticated dashboard plugin mount."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chalkline.ui.api import router
