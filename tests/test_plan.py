from conftest import make_design
from deebot_map_editor import geometry as G
from deebot_map_editor.mapdata import MapState, editor_data
from deebot_map_editor.plan import build_plan, check_divide, wall_steps


def test_g1_index_maps_to_mssid(snap):
    st = MapState.from_snapshot(snap)
    # 组 1 是 0..4 顺序号，mssid 是 0,1,2,4,5
    assert st.g1_keys() == ["0", "1", "2", "3", "4"]
    assert st.index_map() == {"0": "0", "1": "1", "2": "2", "3": "4", "4": "5"}
    d = editor_data(snap)
    g1 = {g["id"]: g for g in d["groups"]}["1"]["rooms"]
    assert set(g1) == {"0", "1", "2", "4", "5"}


def test_no_change_plan_only_backup(snap):
    plan = build_plan(make_design(snap), MapState.from_snapshot(snap))
    assert not plan.errors
    assert [s.phase for s in plan.steps] == ["backup"]


def test_base_signature_mismatch(snap):
    design = make_design(snap)
    snap["rooms"][0][1] = "改过"
    plan = build_plan(design, MapState.from_snapshot(snap))
    assert any("变过" in e for e in plan.errors)


def test_divide_snaps_to_group6(snap):
    st = MapState.from_snapshot(snap)
    ring = st.g6["0"]  # 内缩 50 mm 的矩形 50..4950 × -5950..-2550
    a, b, errs, notes = check_divide(ring, (2500, -6000), (2500, -2500))
    assert not errs
    assert a == [2500, -5950] and b == [2500, -2550]
    assert notes


def test_divide_far_endpoint_extends_along_line(snap):
    ring = MapState.from_snapshot(snap).g6["0"]
    a, b, errs, notes = check_divide(ring, (2500, -5600), (2500, -2900))
    assert not errs and a == [2500, -5950] and b == [2500, -2550]
    assert any("沿切线方向" in n for n in notes)


def test_divide_collinear_rejected(snap):
    ring = MapState.from_snapshot(snap).g6["0"]
    _, _, errs, _ = check_divide(ring, (50, -5950), (50, -2550))
    assert any("共线" in e for e in errs)


def test_divide_outside_rejected(snap):
    ring = MapState.from_snapshot(snap).g6["0"]
    _, _, errs, _ = check_divide(ring, (6000, -5950), (6000, -2550))
    assert errs


def test_wall_steps_split_large_move():
    cur = [[0, 0, "1"], [1000, 0, "1"], [1000, 1000, "1"], [0, 1000, "1"]]
    tgt = [[0, 0, "1"], [1000, 0, "1"], [1000, 2000, "1"], [0, 2000, "1"]]
    steps = wall_steps(cur, tgt, max_move=300)
    assert steps[-1][1] == tgt
    assert len(steps) == 4  # 1000 mm / 300 → 4 步
    prev = cur
    for _, pts in steps:
        assert max(G.dist(p, q) for p, q in zip(pts, prev, strict=False)) <= 300
        prev = pts


def test_wall_steps_insert_and_delete_points():
    cur = [[0, 0, "1"], [1000, 0, "1"], [1000, 1000, "1"], [500, 1000, "2"], [0, 1000, "1"]]
    tgt = [[0, 0, "1"], [1000, 0, "1"], [1000, 500, "1"], [1200, 500, "1"], [1200, 1000, "1"], [0, 1000, "1"]]
    steps = wall_steps(cur, tgt, max_move=300)
    assert steps[-1][1] == tgt
    # 第一步只插点，几何不变：新点都在原来的墙线上
    first = steps[0][1]
    ring = [(p[0], p[1]) for p in cur]
    assert all(G.nearest_on_ring(p, ring)[0] < 1 for p in first)
    # 中间每一步都不能有 0 长度的墙
    for _, pts in steps:
        assert all(G.dist(pts[i - 1], pts[i]) >= 1 for i in range(len(pts)))


def test_plan_with_cut_and_rename_is_provisional(snap):
    def edit(d):
        w = d["walls"]["0"]
        # 设计：客厅沿 x=2500 切开，右半边成为新房间 6
        d["walls"]["0"] = [[x, y, t] for x, y, t in w if x <= 2500] + []
        d["walls"]["0"] = [[0, -6000, "1"], [2500, -6000, "1"], [2500, -2500, "2"], [0, -2500, "2"]]
        d["walls"]["6"] = [[2500, -6000, "1"], [5000, -6000, "1"], [5000, -2500, "2"], [2500, -2500, "2"]]
        d["rooms"]["6"] = {"name": "餐厅", "subtype": "2"}
        d["actions"] = [{"kind": "cut", "mssid": "0", "newId": "6", "p1": [2500, -6000], "p2": [2500, -2500]}]

    plan = build_plan(make_design(snap, edit), MapState.from_snapshot(snap))
    assert not plan.errors, plan.errors
    phases = [s.phase for s in plan.steps]
    assert phases[0] == "backup" and phases[1] == "topology"
    div = plan.steps[1].args["subsets"][0]
    assert div == {"type": "ar", "act": "divide", "mssid": "0", "value": "2500,-5950;2500,-2550"}
    assert phases.index("rename") < phases.index("walls")
    assert all(s.provisional for s in plan.steps if s.phase in ("rename", "walls"))
