from typing import Dict, Optional

from pydantic import BaseModel
import tomli

from .constants import CONFIG_PATH


class Target(BaseModel):
    controller_path: str
    web_path: str
    colors_path: str
    controller_api_url: str

    @property
    def controller_host(self) -> Optional[str]:
        if ":" not in self.controller_path:
            return None
        return self.controller_path.split(":")[0]

    @property
    def controller_fs_path(self) -> str:
        if ":" not in self.controller_path:
            return self.controller_path
        return self.controller_path.split(":")[1]


class Config(BaseModel):
    targets: Dict[str, Target]


def load_config() -> Config:
    with open(CONFIG_PATH, "rb") as config_file:
        return Config.parse_obj(tomli.load(config_file))
