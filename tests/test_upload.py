import asyncio
import json

import pytest
from conftest import make_design, nosleep

from deebot_map_editor.fake import FakeCloud
from deebot_map_editor.upload import NeedReboot, Uploader, UploadStopped


def cut_design(snap):
    def edit(d):
        d["walls"]["0"] = [[-400, -6000, "1"], [2500, -6000, "1"], [2500, -2500, "2"], [-400, -2500, "1"]]
        d["walls"]["6"] = [[2500, -6000, "1"], [5000, -6000, "1"], [5000, -2500, "2"], [2500, -2500, "2"]]
        d["rooms"]["0"]["name"] = "起居室"
        d["rooms"]["6"] = {"name": "餐厅", "subtype": "2"}
        d["actions"] = [{"kind": "cut", "mssid": "0", "newId": "6", "p1": [2500, -6000], "p2": [2500, -2500]}]
    return make_design(snap, edit)


def by_name(state):
    walls = state.walls()
    return {r["name"]: walls[r["mssid"]] for r in state.rooms}


def run(up):
    return asyncio.run(up.run())


def test_full_flow_cut_rename_walls(snap, tmp_path):
    design = cut_design(snap)
    fake = FakeCloud(snap)
    up = Uploader(design, lambda: fake, tmp_path / "j.json", log=lambda m: None, sleep=nosleep)
    state = run(up)
    # 固件给哪一半分配新 mssid 不确定，按名字核对
    w = by_name(state)
    assert w["起居室"] == design["walls"]["0"]
    assert w["餐厅"] == design["walls"]["6"]
    cmds = [c for c, _ in fake.sent if c.startswith("set")]
    # 顺序：备份 → 切割 → 改名 → 写墙
    assert cmds[0] == "setCachedMapInfo"
    assert cmds[1] == "setMapSet_V2" and fake.sent[[c for c, _ in fake.sent].index("setMapSet_V2")][1]["subsets"][0]["act"] == "divide"
    first_wall = cmds.index("setMapInfo_V2")
    assert all(c == "setMapInfo_V2" for c in cmds[first_wall:])
    assert fake.backups == 1
    j = json.loads((tmp_path / "j.json").read_text())
    assert j["backup"] and j["renamed"] and j["finished"]
    assert all(t["status"] == "done" for t in j["topology"])


def test_offline_after_divide_then_resume(snap, tmp_path):
    design = cut_design(snap)
    fake = FakeCloud(snap, drop_after_divide=True)
    log = []
    up = Uploader(design, lambda: fake, tmp_path / "j.json", log=log.append, sleep=nosleep)
    with pytest.raises(NeedReboot):
        run(up)
    fake.reboot()
    fake.drop_after_divide = False
    up2 = Uploader(design, lambda: fake, tmp_path / "j.json", log=log.append, sleep=nosleep)
    state = run(up2)
    assert any("上次已生效" in m for m in log)
    # 只切了一次、只备份了一次
    assert sum(1 for c, a in fake.sent if c == "setMapSet_V2" and a["subsets"][0]["act"] == "divide") == 1
    assert fake.backups == 1
    assert by_name(state)["餐厅"] == design["walls"]["6"]


def test_too_large_step_goes_offline(snap, tmp_path):
    design = cut_design(snap)
    fake = FakeCloud(snap, limit_mm=100)  # 模拟固件很挑剔
    up = Uploader(design, lambda: fake, tmp_path / "j.json", opts={"max_move_mm": 300}, log=lambda m: None, sleep=nosleep)
    with pytest.raises(NeedReboot):
        run(up)
    # 重启后用更小的步长继续，能完成
    fake.reboot()
    up2 = Uploader(design, lambda: fake, tmp_path / "j.json", opts={"max_move_mm": 80}, log=lambda m: None, sleep=nosleep)
    state = run(up2)
    assert by_name(state)["起居室"] == design["walls"]["0"]


def test_merge(snap, tmp_path):
    def edit(d):
        d["actions"] = [{"kind": "merge", "ids": ["2", "4"]}]
        d["rooms"]["2"]["name"] = "厨卫"
    design = make_design(snap, edit)
    fake = FakeCloud(snap)
    up = Uploader(design, lambda: fake, tmp_path / "j.json", log=lambda m: None, sleep=nosleep)
    state = run(up)
    names = {r["mssid"]: r["name"] for r in state.rooms}
    assert "4" not in names and names["2"] == "厨卫"
    # 合并后的房间不写墙（不能用其中一个子房间的墙去覆盖），其它房间的墙恢复成设计
    walls = state.walls()
    assert walls["2"] != design["walls"]["2"]
    for m in ("0", "1", "5"):
        assert walls[m] == design["walls"][m]


def test_bad_divide_stops_before_sending(snap, tmp_path):
    def edit(d):
        d["actions"] = [{"kind": "cut", "mssid": "0", "p1": [50, -5950], "p2": [50, -2550]}]
    design = make_design(snap, edit)
    fake = FakeCloud(snap)
    up = Uploader(design, lambda: fake, tmp_path / "j.json", log=lambda m: None, sleep=nosleep)
    with pytest.raises(UploadStopped):
        run(up)
    assert not any(c.startswith("set") for c, _ in fake.sent)
