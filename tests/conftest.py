import copy
import json
from pathlib import Path

import pytest

from deebot_map_editor.mapdata import editor_data

DEMO = Path(__file__).resolve().parent.parent / "src" / "deebot_map_editor" / "demo-snapshot.json"


@pytest.fixture
def snap():
    return json.loads(DEMO.read_text())


def make_design(snap, edit=None):
    """从快照生成「什么都没改」的设计，再交给 edit 修改。"""
    d = editor_data(snap)
    g1 = {g["id"]: g for g in d["groups"]}["1"]["rooms"]
    design = {
        "kind": "deebot-map-design",
        "mid": snap["mid"],
        "base": d["base"],
        "rooms": {r["mssid"]: {"name": r["name"], "subtype": str(r["icon"])} for r in d["rooms"]},
        "walls": copy.deepcopy(g1),
        "actions": [],
        "extra": [],
    }
    if edit:
        edit(design)
    return design


async def nosleep(_):
    return None
