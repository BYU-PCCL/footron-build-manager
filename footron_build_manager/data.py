from typing import Dict

from pydantic import BaseModel
import json

from .constants import DATA_PATH


class Target(BaseModel):
    hashes: Dict[str, str] = {}


class BuildData(BaseModel):
    targets: Dict[str, Target] = {}


def load_build_data() -> BuildData:
    if not DATA_PATH.exists():
        return BuildData(targets={})

    with open(DATA_PATH, "r") as data_file:
        return BuildData.parse_obj(json.load(data_file))


def save_build_data(build_data: BuildData) -> None:
    if not DATA_PATH.exists():
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_PATH.touch()

    with open(DATA_PATH, "w") as data_file:
        json.dump(build_data.dict(), data_file)
