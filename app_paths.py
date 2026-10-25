"""Keep portable user data beside the EXE, separate from bundled resources."""
from pathlib import Path
import sys


APP_DIRECTORY = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                 else Path(__file__).resolve().parent)
