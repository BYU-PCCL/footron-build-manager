import os
from pathlib import Path

from xdg import XDG_CONFIG_HOME, XDG_DATA_HOME


GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET")
GITHUB_ACCESS_TOKEN = os.environ.get("GITHUB_ACCESS_TOKEN")

CONFIG_PATH = Path(
    os.environ.get("FT_CONFIG_PATH")
    or XDG_CONFIG_HOME / "footron" / "build-config.toml"
)
DATA_PATH = Path(
    os.environ.get("FT_DATA_PATH") or XDG_DATA_HOME / "footron" / "build.json"
)
