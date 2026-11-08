"""scripts/plan_layout.py: link flags, receiver budgets, coverage counts, sides, zones, accuracy and suggestions.

The layouts are the planning examples in config/examples/. The tool itself is standard library only; one test also
loads every example through csi.room to check that the room loader ignores the planning keys.
"""
import json
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "config" / "examples"
sys.path.insert(0, str(ROOT / "scripts"))
import plan_layout  # noqa: E402

LAYOUTS = ("current_rig", "three_room_row", "open_floor_8x6", "l_house")
ROOM = {"width_m": 4.0, "depth_m": 4.0}


def plan(name, **options):
    return plan_layout.analyse(plan_layout.load_layout(EXAMPLES / f"{name}.json"), **options)


def link(result, name):
    return next(lk for lk in result["links"] if lk["name"] == name)


def count_at(result, x, y):
    """Links covering the grid cell that contains (x, y); None for a cell outside the footprint."""
    cov = result["coverage"]
    c = math.floor((x - cov["origin"][0]) / cov["cell_m"])
    r = math.floor((y - cov["origin"][1]) / cov["cell_m"])
    return cov["counts"][r][c]


def suggestion(result, kind, **match):
    return next(s for s in result["suggestions"] if s["kind"] == kind and all(s.get(k) == v for k, v in match.items()))


# --- the examples and the room loader ------------------------------------------------------------------------------

@pytest.mark.parametrize("name", LAYOUTS)
def test_room_loader_reads_every_example_and_ignores_the_planning_keys(name):
    from csi.room import load_room

    room = load_room(EXAMPLES / f"{name}.json")
    layout = plan_layout.load_layout(EXAMPLES / f"{name}.json")
    assert {n.name for n in room.receivers} == {n.name for n in layout.nodes if n.role == "receiver"}
    assert {n.name for n in room.senders} == {n.name for n in layout.nodes if n.role == "sender"}
    assert len(room.links()) == len(plan(name, search=False)["links"])


def test_current_rig_example_matches_the_rig_nodes():
    layout = plan_layout.load_layout(EXAMPLES / "current_rig.json")
    positions = {n.name: (n.role, n.x, n.y) for n in layout.nodes}
    assert positions == {"rx1": ("receiver", 2.77, 0.0), "rx2": ("receiver", 0.0, 0.0),
                         "tx0": ("sender", 2.77, 1.84), "tx1": ("sender", 0.0, 1.84)}
    assert layout.disabled == ["rx3"] and layout.send_hz == 20 and not layout.footprint_given


# --- per link: RSSI, walls and the four flags ----------------------------------------------------------------------

def test_rssi_is_minus_32_dbm_at_2_m_less_20_db_per_decade_and_the_walls():
    assert plan_layout.predict_rssi(2.0) == -32.0
    assert plan_layout.predict_rssi(20.0, wall_db=10) == pytest.approx(-62.0)
    rig = plan("current_rig")
    assert link(rig, "rx2<-tx1")["rssi_dbm"] == pytest.approx(-32 - 20 * math.log10(1.84 / 2), abs=0.05)
    assert link(rig, "rx2<-tx0")["rssi_dbm"] == pytest.approx(-32 - 20 * math.log10(math.hypot(2.77, 1.84) / 2),
                                                              abs=0.05)


def test_current_rig_same_wall_links_are_same_side_and_the_diagonals_are_clean():
    rig = plan("current_rig")
    assert {lk["name"] for lk in rig["links"]} == {"rx1<-tx0", "rx1<-tx1", "rx2<-tx0", "rx2<-tx1"}
    assert link(rig, "rx2<-tx1")["flags"] == ["SAME_SIDE"]        # both on the west wall
    assert link(rig, "rx1<-tx0")["flags"] == ["SAME_SIDE"]        # both on the east wall
    assert link(rig, "rx1<-tx1")["flags"] == [] and link(rig, "rx2<-tx0")["flags"] == []
    assert link(rig, "rx1<-tx1")["label"] == "rx1<-01" and link(rig, "rx1<-tx1")["weight"] == 0.3


def test_three_room_row_counts_the_walls_and_flags_the_longest_diagonals_weak():
    row = plan("three_room_row")
    straight, far = link(row, "rx1<-tx0"), link(row, "rx1<-tx3")
    assert straight["walls"] == [0, 2] and straight["wall_db"] == 20       # north and south brick
    assert far["walls"] == [0, 2, 4, 5] and far["wall_db"] == 28           # and both drywall partitions
    assert far["rssi_dbm"] == pytest.approx(-32 - 20 * math.log10(math.hypot(9.0, 6.8) / 2) - 28, abs=0.05)
    assert sorted(lk["name"] for lk in row["links"] if "WEAK" in lk["flags"]) == ["rx1<-tx3", "rx4<-tx0"]
    assert straight["flags"] == []
    # Boards 0.9 m outside a 5 m deep row: a quarter of each link is outside, all of it the ends OUTSIDE allows.
    assert straight["outside_share_raw"] == pytest.approx(1.8 / 6.8, abs=1e-3) and straight["outside_share"] == 0


def test_l_house_flags_the_notch_the_bunched_corner_pair_and_the_same_side_pairs():
    house = plan("l_house")
    notch = link(house, "rx3<-tx0")                 # east of the kitchen, across the notch, to south of the bedroom
    assert "OUTSIDE" in notch["flags"] and notch["outside_share"] > 0.5
    corner = link(house, "rx3<-tx1")                # two boards 1.3 m apart by the corner of the notch
    assert set(corner["flags"]) == {"OUTSIDE", "SHORT"} and corner["length_m"] < 1.5 and corner["cells"] == 0
    for name in ("rx1<-tx3", "rx2<-tx3", "rx4<-tx2"):
        assert set(link(house, name)["flags"]) == {"SAME_SIDE", "OUTSIDE"} and link(house, name)["cells"] == 0
    weak = link(house, "rx2<-tx0")                  # 5 walls: north, partition, notch, wing east, wing south
    assert "WEAK" in weak["flags"] and weak["wall_db"] == 44
    # Cutting the north-west corner with both boards 0.9 m out is fine: its outside part is the allowed ends.
    assert link(house, "rx1<-tx2")["flags"] == [] and link(house, "rx1<-tx2")["outside_share_raw"] > 0.5


def test_same_side_needs_both_boards_by_one_edge_and_the_link_along_it():
    layout = plan_layout.load_layout({"room": ROOM, "nodes": {
        "rx1": {"role": "receiver", "x": 0.0, "y": -0.5}, "rx2": {"role": "receiver", "x": 2.0, "y": 5.0},
        "tx0": {"role": "sender", "x": 4.0, "y": -1.4}, "tx1": {"role": "sender", "x": 0.8, "y": -1.4}}})
    r = plan_layout.analyse(layout, search=False)
    assert "SAME_SIDE" in link(r, "rx1<-tx0")["flags"]          # both north of the room, 13 degrees off the wall
    steep = link(r, "rx1<-tx1")                                 # both north too, but 48 degrees off the wall
    assert "SAME_SIDE" not in steep["flags"] and "SHORT" in steep["flags"]
    assert "SAME_SIDE" not in link(r, "rx2<-tx0")["flags"]      # south side to north side, across the room
    assert "SAME_SIDE" not in link(r, "rx2<-tx1")["flags"]


def test_outside_share_does_not_count_the_ends_where_boards_stand_outside():
    square = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    raw, counted = plan_layout.outside_parts((-1.0, 2.0), (5.0, 2.0), square)
    assert raw == pytest.approx(2 / 6) and counted == 0
    raw, counted = plan_layout.outside_parts((-3.0, 2.0), (7.0, 2.0), square)    # 3 m out at each end
    assert raw == pytest.approx(0.6) and counted == pytest.approx(0.3)
    assert plan_layout.outside_parts((-1.0, -1.0), (5.0, -1.0), square) == (1.0, 1.0)   # never enters


def test_walls_take_db_or_a_named_material():
    layout = plan_layout.load_layout(EXAMPLES / "l_house.json")
    assert [w.db for w in layout.walls] == [10.0] * 6 + [4.0] * 2
    with pytest.raises(plan_layout.LayoutError, match="unknown material"):
        plan_layout.load_layout({"room": ROOM, "nodes": {}, "walls": [{"a": [0, 0], "b": [1, 0], "material": "tin"}]})


# --- per receiver: gain spread and serial load ---------------------------------------------------------------------

def test_three_room_row_receivers_warn_on_rssi_spread_but_not_on_serial_load():
    row = plan("three_room_row")
    rx = {r["name"]: r for r in row["receivers"]}
    assert rx["rx1"]["spread_db"] == pytest.approx(12.4, abs=0.05)
    assert (rx["rx1"]["strongest"], rx["rx1"]["weakest"]) == ("tx0", "tx3")
    assert all("RSSI_SPREAD" in r["warnings"] for r in rx.values())
    assert rx["rx1"]["lines_per_s"] == pytest.approx(4 * plan_layout.real_rate(20), abs=0.05)
    assert not any(w.startswith("SERIAL") for r in rx.values() for w in r["warnings"])
    assert "CSI_GAIN_LOCK_STRONGEST" in suggestion(row, "rssi_spread")["text"]
    assert all(r["spread_db"] < 6 for r in plan("current_rig")["receivers"])


def test_serial_load_uses_the_real_send_rate_and_warns_over_80_and_over_the_cap():
    assert plan_layout.real_rate(20) == pytest.approx(19.6, abs=0.05)
    assert plan_layout.real_rate(40) == pytest.approx(38.46, abs=0.01)
    fast = plan("three_room_row", send_hz=40)          # 4 senders x 38.5 = 154 lines/s
    assert all("SERIAL_OVER_CAP" in r["warnings"] for r in fast["receivers"])
    serial = suggestion(fast, "serial")
    assert serial["rank"] == 1 and "CSI_SEND_HZ to 20" in serial["text"]
    busy = plan("three_room_row", send_hz=25)          # 4 x 24.4 = 98 lines/s
    assert all("SERIAL_HIGH" in r["warnings"] for r in busy["receivers"])


# --- coverage ------------------------------------------------------------------------------------------------------

def test_one_link_covers_a_band_of_its_half_width_without_end_caps():
    across = plan_layout.analyse(plan_layout.load_layout({"room": ROOM, "nodes": {
        "rx1": {"role": "receiver", "x": -1.0, "y": 2.0}, "tx0": {"role": "sender", "x": 5.0, "y": 2.0}}}),
        search=False)
    assert across["links"][0]["band_half_width_m"] == pytest.approx(0.9)      # 0.6 m + 5% of 6 m
    covered = [row for row in across["coverage"]["counts"] if any(row)]
    assert len(covered) == 8 and all(row == [1] * 16 for row in covered)      # cell centres 1.125 to 2.875 m
    inside = plan_layout.analyse(plan_layout.load_layout({"room": ROOM, "nodes": {
        "rx1": {"role": "receiver", "x": 2.0, "y": 2.0}, "tx0": {"role": "sender", "x": 5.0, "y": 2.0}}}),
        search=False)
    assert count_at(inside, 2.1, 2.1) == 1 and count_at(inside, 1.9, 2.1) == 0   # nothing behind the receiver
    assert inside["links"][0]["cells"] == 6 * 8


def test_current_rig_coverage_counts_match_a_hand_count():
    rig = plan("current_rig")
    cov = rig["coverage"]
    assert (cov["cols"], cov["rows"], cov["cells"]) == (12, 8, 77)     # cell centres inside 2.77 x 1.84 m
    assert count_at(rig, 1.385, 0.92) == 2        # the diagonals cross at the centre; the wall links' bands miss it
    assert count_at(rig, 0.1, 0.8) == 3           # the west wall link and both diagonals
    assert count_at(rig, 2.8, 0.5) is None        # beyond the last cell centre inside the room
    assert cov["blind"] == 65 and cov["blind"] == len(cov["blind_cells"])


def test_band_matches_a_brute_force_scan():
    rng = random.Random(7)
    grid = plan_layout.Grid([(0, 0), (10, 0), (10, 4), (4, 4), (4, 8), (0, 8)], 0.25)
    cases = [((-1.0, 2.0), (11.0, 2.0), 0.7), ((3.0, -1.0), (3.0, 9.0), 0.6), ((2.0, 2.0), (2.0, 2.0), 1.0)]
    cases += [((rng.uniform(-2, 12), rng.uniform(-2, 10)), (rng.uniform(-2, 12), rng.uniform(-2, 10)),
               rng.uniform(0.1, 2.0)) for _ in range(150)]
    for a, b, w in cases:
        dx, dy = b[0] - a[0], b[1] - a[1]
        l2 = dx * dx + dy * dy
        brute = [] if l2 < 1e-12 else [
            i for i, (x, y) in enumerate(grid.centres)
            if 0 <= (x - a[0]) * dx + (y - a[1]) * dy <= l2
            and abs((x - a[0]) * dy - (y - a[1]) * dx) <= w * math.sqrt(l2) + 1e-9]
        assert grid.band(a, b, w) == brute


def test_l_house_cells_follow_the_l_and_the_map_leaves_the_notch_blank(capsys):
    house = plan("l_house")
    assert house["coverage"]["cells"] == 56 * 16          # 56 m2 of 0.25 m cells
    assert count_at(house, 7.0, 6.0) is None and count_at(house, 2.0, 6.0) is not None
    assert plan_layout.main([str(EXAMPLES / "l_house.json")]) == 0
    rows = map_rows(capsys.readouterr().out)
    # The map starts one cell beyond the westmost and northmost boards (both at -0.9 m, cell -4).
    assert rows[24 + 5][28 + 5] == "."                   # (7, 6) in the notch
    assert rows[24 + 5][8 + 5].isdigit()                 # (2, 6) in the bedroom


def test_transceiver_mode_links_every_ordered_pair_and_counts_each_path_once():
    both = plan("open_floor_8x6", mode="transceiver")
    assert len(both["links"]) == 56 and both["paths"] == 28
    assert all(r["senders"] == 7 and "SERIAL_OVER_CAP" in r["warnings"] for r in both["receivers"])  # 137 lines/s
    ab, ba = link(both, "rx1<-tx0"), link(both, "tx0<-rx1")
    assert ab["cells"] == ba["cells"] and ab["rssi_dbm"] == ba["rssi_dbm"]
    unique = {tuple(sorted((lk["receiver"], lk["sender"]))): lk["cells"] for lk in both["links"]}
    total = sum(c for row in both["coverage"]["counts"] for c in row if c is not None)
    assert total == sum(unique.values())
    same = {tuple(sorted((lk["receiver"], lk["sender"]))) for lk in both["links"] if "SAME_SIDE" in lk["flags"]}
    assert same == {("rx1", "rx2"), ("rx3", "rx4"), ("tx0", "tx1"), ("tx2", "tx3")}
    assert both["coverage"]["blind"] < plan("open_floor_8x6")["coverage"]["blind"]


def test_max_crossing_angle_between_lines():
    assert plan_layout._max_crossing([0, 10, 20]) == pytest.approx(20)
    assert plan_layout._max_crossing([0, 90]) == pytest.approx(90)
    assert plan_layout._max_crossing([170, 10]) == pytest.approx(20)
    assert plan_layout._max_crossing([5]) == 0


# --- zones, sides and accuracy -------------------------------------------------------------------------------------

def test_zone_link_counts_and_board_diversity():
    row = {z["name"]: z for z in plan("three_room_row")["zones"]}
    assert (row["west room"]["links"], row["middle room"]["links"], row["east room"]["links"]) == (5, 10, 5)
    assert row["middle room"]["blind_cells"] == 0 and row["west room"]["blind_cells"] == 150
    assert row["west room"]["receivers"] == ["rx1", "rx2", "rx3"] and row["west room"]["senders"] == ["tx0", "tx1",
                                                                                                      "tx2"]
    floor = {z["name"]: z for z in plan("open_floor_8x6")["zones"]}
    assert floor["north-west"]["links"] == 4 and floor["north-west"]["receivers"] == ["rx1", "rx3"]
    assert floor["south-east"]["senders"] == ["tx1", "tx3"]
    house = {z["name"]: z for z in plan("l_house")["zones"]}
    assert house["bedroom"]["link_names"] == ["rx1<-tx0", "rx2<-tx0", "rx4<-tx1"]


def test_a_zone_with_too_few_links_gets_a_board_aimed_at_it():
    row = plan("three_room_row", min_links=6)
    zones = {s["zone"]: s for s in row["suggestions"] if s["kind"] == "zone"}
    assert set(zones) == {"west room", "east room"}
    west = zones["west room"]
    assert west["text"].startswith("Zone west room is covered by 5 links (6 or more")
    assert west["add"]["y"] < 0 and west["add"]["x"] < 4             # outside the north wall of that room


def test_sides_covered():
    row, house = plan("three_room_row"), plan("l_house")
    assert row["sides"]["covered"] == ["north", "south"] and row["sides"]["missing"] == ["east", "west"]
    assert row["suggestions"][0]["kind"] == "sides"
    assert plan("open_floor_8x6")["sides"]["count"] == 4
    assert plan("current_rig")["sides"]["count"] == 4                   # corner boards count for both walls
    assert house["sides"]["count"] == 4 and (house["sides"]["edges_covered"], house["sides"]["edges"]) == (5, 6)


def test_accuracy_class_follows_perimeter_spacing_with_caps():
    ac = plan_layout.accuracy_class
    assert ac(30.0, 30, 4, 0.0)["class"] == "sub-metre"                 # 1 m spacing
    assert ac(40.0, 20, 4, 0.0)["class"] == "about 1-1.5 m"             # 2 m
    assert ac(30.0, 10, 4, 0.0)["class"] == "room level"                # 3 m
    assert ac(40.0, 10, 4, 0.0)["class"] == "presence per zone at best"  # 4 m
    assert ac(30.0, 30, 2, 0.0)["class"] == "room level"                # boards on two sides only
    assert ac(9.0, 9, 4, 0.0)["class"] == "room level"                  # fewer than 10 boards
    assert ac(40.0, 20, 4, 0.5)["class"] == "room level"                # half the cells blind
    assert plan("open_floor_8x6")["spacing"]["spacing_m"] == pytest.approx(3.5)
    assert plan("three_room_row")["spacing"]["class"] == "presence per zone at best"


# --- suggestions ---------------------------------------------------------------------------------------------------

def test_current_rig_suggestions_move_a_sender_off_each_shared_wall_and_plan_additions():
    rig = plan("current_rig")
    same = [s for s in rig["suggestions"] if s["kind"] == "same_side"]
    assert {(s["node"], tuple(s["partners"])) for s in same} == {("tx0", ("rx1",)), ("tx1", ("rx2",))}
    assert "tx0 and rx1 share the east wall" in suggestion(rig, "same_side", node="tx0")["text"]
    for s in same:
        x, y = s["move_to"]["x"], s["move_to"]["y"]
        assert s["move_to"]["blind_after"] < rig["coverage"]["blind"]
        assert not (0 <= x <= 2.77 and 0 <= y <= 1.84)                   # outside the room
    add = suggestion(rig, "add_boards")
    assert add["rank"] == 1 and add["plan"][0]["blind_after"] < rig["coverage"]["blind"]
    assert [s["rank"] for s in rig["suggestions"]] == list(range(1, len(rig["suggestions"]) + 1))


def test_l_house_suggestions_name_the_board_sharing_the_north_wall():
    house = plan("l_house")
    tx3 = suggestion(house, "same_side", node="tx3")
    assert tx3["partners"] == ["rx1", "rx2"] and "tx3 shares the north wall with rx1 and rx2" in tx3["text"]
    kinds = [s["kind"] for s in house["suggestions"]]
    assert {"outside", "weak", "short", "rssi_spread"} <= set(kinds)
    # A same-side link is reported as same-side, not again as outside.
    assert "rx1<-tx3" not in suggestion(house, "outside")["links"]


def test_a_layout_without_senders_still_reports():
    lonely = plan_layout.analyse(plan_layout.load_layout(
        {"room": ROOM, "nodes": {"rx1": {"role": "receiver", "x": -0.8, "y": 2.0}}}))
    assert lonely["links"] == [] and lonely["coverage"]["blind"] == lonely["coverage"]["cells"]
    assert lonely["suggestions"][0]["kind"] == "no_links"
    # One more board makes one link, which cannot clear a cell that needs three: no addition is suggested.
    assert not any(s["kind"] == "add_boards" for s in lonely["suggestions"])
    assert plan_layout.render_text(lonely).startswith("Layout plan: ")


# --- output and errors ---------------------------------------------------------------------------------------------

def map_rows(report):
    """The coverage map's rows (without the y labels) from a text report."""
    lines = report.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Coverage: links per cell"))
    end = next(i for i in range(start, len(lines)) if " cells; blind" in lines[i])
    return [line[6:] for line in lines[start + 3:end]]


@pytest.mark.parametrize("name", LAYOUTS)
def test_text_report_has_a_map_of_digits_and_numbered_suggestions(name, capsys):
    assert plan_layout.main([str(EXAMPLES / f"{name}.json")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Layout plan: ")
    rows = map_rows(out)
    assert len({len(row) for row in rows}) == 1 and set("".join(rows)) <= set("0123456789+.RS*")
    assert set(rows[0]) == {"."} and set(rows[-1]) == {"."}           # a margin of outside cells all round
    assert any(ch.isdigit() for row in rows for ch in row) and "R" in "".join(rows) and "S" in "".join(rows)
    tail = out[out.index("Suggestions (most important first)"):]
    assert "\n  1. " in tail


def test_json_output_is_the_full_result(capsys):
    assert plan_layout.main([str(EXAMPLES / "l_house.json"), "--json", "--mode", "transceiver"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "transceiver" and len(data["links"]) == 56
    assert data["coverage"]["blind"] == len(data["coverage"]["blind_cells"])
    assert {"links", "receivers", "coverage", "zones", "sides", "spacing", "suggestions"} <= set(data)


@pytest.mark.parametrize("config, message", [
    ({"nodes": {}}, 'missing "room"'),
    ({"room": ROOM, "nodes": {}, "footprint": [[0, 0], [1, 1]]}, "at least 3 points"),
    ({"room": ROOM, "nodes": {}, "footprint": [[0, 0], [1, 1], [2, 2]]}, "no area"),
    ({"room": ROOM, "nodes": {}, "zones": {"a": [0, 0, 0, 1]}}, "no area"),
    ({"room": ROOM, "nodes": {}, "walls": [{"a": [0, 0], "b": [1, 0]}]}, '"db"'),
    ({"room": ROOM, "nodes": {"rx1": {"role": "receiver", "x": "1", "y": 0}}}, "expected a number"),
    ({"room": ROOM, "nodes": {"rx1": {"role": "receiver", "x": 1, "y": 0, "enabled": "no"}}}, "enabled"),
])
def test_bad_layouts_are_refused_with_a_reason(config, message):
    with pytest.raises(plan_layout.LayoutError, match=message):
        plan_layout.load_layout(config)


def test_command_line_errors_exit_2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert plan_layout.main([str(bad)]) == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert plan_layout.main([str(EXAMPLES / "current_rig.json"), "--grid", "0.01"]) == 2
    assert "--grid" in capsys.readouterr().err
