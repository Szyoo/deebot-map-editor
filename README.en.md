# deebot-map-editor

English · [中文](README.md)

A map viewer and editor for ECOVACS DEEBOT robot vacuums. Edit room walls, split and merge rooms and rename them in a local web page, then upload everything to the robot in one go. The upload is ordered according to how the firmware actually behaves and broken into small steps, and every step is read back and verified.

Tested on a DEEBOT T80S PRO (class `sd3tgp`, China cloud). Other models that use the V2 map protocol (`getMapInfo_V2` / `setMapSet_V2`) should work in principle but have not been verified.

> ⚠️ Writing maps is risky. A backup is made automatically before uploading, but group 1 (room walls) is not covered by backup/restore. Try the demo mode first, then start with small changes on a real robot.

The user interface is currently in Chinese only.

## Features

- **View**: room walls (group 1), laser cleaning boundaries (group 6), App outlines (group 2), raw mapping outlines (type 7), virtual walls, carpets, door sills, patrol points and raster fragments, each on its own toggleable layer.
- **Edit**:
  - drag corners and walls, draw new walls, double-click to add a corner;
  - change wall type (outer / inner / door segment), insert doors quickly;
  - split overlapping corners, merge collinear points automatically;
  - split and merge rooms, rename rooms and change room type;
  - history, checkpoints, consistency checks between neighbouring rooms;
  - show the length (mm) of every wall segment and every corner angle on the map, each with its own toggle.
- **Background image**: put a floor plan (or any image) under the map and adjust walls against it:
  - opacity, rotation, scale, position, horizontal/vertical mirroring;
  - "two-point align": click two reference points on the image, then the matching points on the map — scale, angle and position are solved in one step;
  - "two-point scale": click both ends of a known length and type in the real length;
  - the image is stored only in your browser and is never sent to the robot or any server.
- **Dry-run**: lists the complete command sequence an upload would send, with validation:
  - split-line endpoints snap to the cleaning boundary, and lines collinear with the boundary are rejected;
  - design rooms are matched to robot rooms by position;
  - wall writes are broken into several steps.
- **One-click upload**: backup → split/merge → rename → write walls room by room in small steps, reading back after every step. If the robot drops offline you are asked to reboot it, and the upload resumes from where it stopped.
- **Demo mode**: a fictional map and a simulated robot, no robot needed.

## Install

Requires Python 3.14 or newer (deebot-client 18 needs it).

```bash
git clone https://github.com/Szyoo/deebot-map-editor.git
cd deebot-map-editor
python3 -m venv .venv
.venv/bin/pip install -e .
```

Uses [deebot-client](https://github.com/DeebotUniverse/client.py) to log in to the ECOVACS cloud and send commands. If your model is not in its supported list, this tool borrows the description of a similar model to connect; since it only sends raw commands, this does not affect it.

## Credentials

Credentials are read from environment variables only:

| Variable | Meaning |
|---|---|
| `ECOVACS_USERNAME` | ECOVACS account |
| `ECOVACS_PASSWORD` | password |
| `ECOVACS_COUNTRY` | country/region code, e.g. `CN`, `DE`, `US` |
| `DEEBOT_DID` / `DEEBOT_CLASS` | optional; pick a device when the account has several |

You can also put them in a file outside the repository (one `KEY=VALUE` per line):

- `~/.config/deebot-map-editor/credentials.env` is read by default;
- or pass `--env-file PATH`.

**Never commit the credentials file.** The program does not print credentials or write them to any file.

## Run

```bash
# demo mode: fictional map + simulated robot
.venv/bin/deebot-map serve --demo

# real robot: click "从机器人读取" (read from robot, read-only) in the page
.venv/bin/deebot-map serve
```

The browser opens http://127.0.0.1:8790/ automatically. The server only listens on the local machine and only accepts requests addressed to `127.0.0.1` / `localhost`.

The "上传到机器人" (upload) panel on the right has three buttons:

1. **Dry-run**: lists every command that would be sent, sends nothing. Uploading is blocked while there are errors.
2. **Start upload**: in the dry-run dialog; runs after confirmation, with live progress and log.
3. **Continue**: after the robot dropped offline, reboot it physically and click "机器人已重启，继续". The tool waits for the robot to come back online and resumes.

### Background image

Pick an image under "背景图（户型图）" on the left. It is first fitted to the map extent. Then:

1. Click "两点对齐" (two-point align), click two easily recognisable points on the image (e.g. two outer corners), then click where they are on the map (snaps to nearby wall corners).
2. For fine tuning, tick "调整背景图" (adjust background): drag to move, ⌥+wheel to scale, ⇧+wheel to rotate, or type values. The map itself is locked while adjusting; untick to edit again.
3. Turn on the "墙长（mm）" (wall length) and "墙角角度" (corner angle) layers and drag walls to match the dimensions on the plan.

The image and its placement are saved per map in the browser and restored next time.

### Command line

```bash
deebot-map fetch                         # read the map, save a snapshot (read-only)
deebot-map plan  design.json             # dry-run: list the command sequence
deebot-map upload design.json            # also only a dry-run
deebot-map upload design.json --yes      # really upload (asks you to type yes)
deebot-map upload design.json --demo --yes   # run against the simulated robot
deebot-map wait-online                   # wait for the robot to come back online
```

`design.json` is produced by the "导出设计文件" (export design) button. `examples/demo-design.json` is a design for the demo map (splits the living room, pushes out a wall, renames rooms):

```bash
deebot-map plan examples/demo-design.json --demo
deebot-map upload examples/demo-design.json --demo --yes
```

Snapshots and upload journals live in `~/.local/share/deebot-map-editor/` by default; change it with `--data-dir` or `DEEBOT_MAP_DATA`.

## How it works

### Map data

| Data | Read with | Notes |
|---|---|---|
| Room list | `getMapSet_V2 {type:"ar"}` | mssid, name, type, centre, … |
| Group 1: room walls | `getMapInfo_V2 {type:"0"}` | one segment per room, `index;x,y,T;x,y,T;…`. **The leading number is a sequential index, not the mssid** |
| Group 2: App outline | same | starts with the mssid |
| Group 6: cleaning boundary | same | starts with the mssid. Generated from laser data; decides how rooms are actually divided |
| Type 7: raw outline | `getMapInfo_V2 {type:"7"}` | room outlines from the mapping run |

Conventions:

- Coordinates are in mm; up is negative y.
- `T` on a wall point is the type of the wall segment *ending* at that point: `1` outer wall, `2` inner wall, anything else (e.g. `3-1-0`) a door segment.
- `info` / `subsets` are base64-encoded zstd- or LZMA-compressed JSON.
- The group 1 index follows the order of the room list. When mssids are not contiguous (the demo map uses 0, 1, 2, 4, 5) they must be translated; the tool does this on both read and write.

### Upload order

1. **Backup**: `setCachedMapInfo {act:"backup"}`.
2. **Split / merge**: `setMapSet_V2` with `act` `divide` or `merge`.
   - Both make the firmware regenerate group 1 for the whole map and clear the names of the affected rooms, so they must come first.
   - Both endpoints of a divide line must lie on the group 6 boundary of the room being split, within about 10 cm. Endpoints slightly off are snapped; endpoints further away are extended along the line until they hit the boundary.
   - A line that coincides with a group 6 edge fails (error 20004). A line parallel to an edge one grid cell (25 mm) away works.
   - `value` uses device coordinates, y not negated.
   - After each operation the map is read again and the room count is checked.
3. **Rename**: design rooms are matched to robot mssids by geometry (sample points inside each design room are tested against the robot's group 6), then `rename` is sent for each.
   - After a split, which half gets the new id is not deterministic, so matching by position is the only reliable way.
4. **Write walls**: `setMapInfo_V2 {type:"1", info:["1", one segment per room…]}`.
   - `info` must be a flat array; a nested array clears group 1.
   - Changes that are too large make the robot stop responding, drop offline and discard the write. So the tool changes one room at a time and splits big changes into steps:
     1. insert the new corners onto the existing walls (geometry unchanged);
     2. move corners in several steps, no single corner moving more than "max move per step" (default 300 mm);
     3. change segment types;
     4. remove the now-redundant corners (they already lie on the target walls).
   - After each step the tool waits about 6 seconds and reads group 1 back; it only continues if it matches exactly.

### Going offline and resuming

Every completed step is written to the upload journal. The tool stops in two cases:

- **The robot does not respond**: you are asked to reboot the robot. Once it is back online, run the same design again and it resumes.
  - If it stopped on a split, the room count tells whether the split took effect: if so it is skipped, otherwise it is sent again.
  - Renames and wall writes are always recomputed from the robot's current state, so finished steps are never sent twice.
- **Read-back mismatch**: the firmware did not accept the step. Lower "max move per step" and upload again.

### Editing rules

The App restricts wall dragging quite a lot (outward only, walls at least 0.5 m, inner walls and doors fixed, corner count fixed). The firmware does not enforce these, so the editor does not warn about them.

The editor still flags things that are likely real problems:

- self-intersecting walls;
- zero-length walls;
- overlapping rooms;
- mismatched wall types on the two sides of a shared wall.

### Other commands

Commands for virtual walls, carpets, door sills, materials, patrol points and furniture were reverse-engineered from the App and have not been tested on a real robot. They are not sent by default; tick "未实测命令：一起发送" (also send untested commands) to send them after renaming and before writing walls.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/ruff check .
python examples/make_demo.py      # regenerate the demo map
```

GitHub Actions runs ruff and the tests on every push and pull request.

| File | Contents |
|---|---|
| `src/deebot_map_editor/mapdata.py` | decoding, snapshot format, group 1 index ↔ mssid mapping, editor data |
| `src/deebot_map_editor/plan.py` | upload planning (pure functions): split-line checks, room matching, wall step splitting |
| `src/deebot_map_editor/upload.py` | executor: journal, read-back checks, offline handling |
| `src/deebot_map_editor/cloud.py` | ECOVACS cloud session |
| `src/deebot_map_editor/fake.py` | simulated robot (demo and tests) |
| `src/deebot_map_editor/server.py` | local web server |
| `src/deebot_map_editor/web/editor.html` | single-page editor |

## License

MIT
