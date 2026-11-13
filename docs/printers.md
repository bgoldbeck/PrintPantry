# Printers (v2)

The **Printers** page (top bar) shows each printer's state, temperatures,
loaded filament per toolhead, job progress and a camera picture that refreshes
about every 3 seconds. A sliced file on a model page gets a **Print on ...**
button when its printer is connected.

## Snapmaker U1

Talks to Moonraker at `PP_U1_URL` (`http://192.168.1.52:7125` in the compose
files). Stock firmware needs no login.

* **Status:** `GET /printer/objects/query` every 3 s (10 s while offline):
  `print_stats`, `virtual_sdcard`, `display_status`, `extruder`..`extruder3`,
  `heater_bed`, `toolhead`, `webhooks`, `print_task_config`.
  Loaded filament comes from `print_task_config` (`filament_type`,
  `filament_color_rgba`, `filament_vendor`, `filament_exist`).
* **Camera:** stock firmware only refreshes `camera/monitor.jpg` after the
  Snapmaker websocket call `camera.start_monitor {"domain":"lan","interval":0}`
  (it isn't available over HTTP). The app sends it at most every 20 s while
  someone is looking at a camera, then serves
  `/server/files/camera/monitor.jpg` through `/api/printers/u1/snapshot`.
* **Starting a print:** the dialog shows a fresh camera picture, the file's
  filament per toolhead next to what's loaded, and needs a "bed is clear" tick
  (plus a second tick if any toolhead looks wrong or empty). The server then
  re-checks the printer is idle, uploads the `.gcode` to the `gcodes` root
  without starting it, checks again, and calls `/printer/print/start`.
  Files sliced for another printer, or a busy/offline printer, are refused.
* **Which toolhead prints what:** the dialog suggests a toolhead for each of the
  file's filaments (its own slot if the material matches, else a free loaded head
  with that material) and lets you change it. The start then goes the way
  Snapmaker Orca does it: `SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=<file> MAP_EXTRUDER=<head>`
  per filament, `SET_PRINT_USED_EXTRUDERS EXTRUDERS=<heads>`, then the websocket call
  `server.files.start_local_print` with the same `map_table` (firmware older than 1.3.0
  falls back to `/printer/print/start`).
* **Controls:** pause, resume, cancel (with a confirm).
* **Calibration:** bed mesh, input shaper, toolhead offsets and a filament
  flow ratio test; see [calibration.md](calibration.md).

## Bambu P1S

Needs LAN-only + Developer Mode on the printer. Settings: `PP_P1S_HOST`,
`PP_P1S_SERIAL`, `PP_P1S_ACCESS_CODE`. On TrueNAS they live only in
`src/.pp_p1s` (`HOST=`, `SERIAL=`, `ACCESS_CODE=` lines) and `deploy/truenas.sh`
fills them into the compose file, like the login password. Code:
`app/printers/bambu.py`.

* **Status:** MQTT over TLS on 8883 (user `bblp`, password = access code),
  topic `device/<serial>/report`. A full `pushall` on connect and every 5 min;
  partial updates in between are merged. `gcode_state` IDLE/PREPARE/RUNNING/
  PAUSE/FINISH/FAILED maps to idle/starting/printing/paused/complete/failed
  (a stop shows as cancelled).
* **"Toolheads" are AMS trays:** index = AMS id × 4 + slot, shown as A1..A4
  (B1.. for a second AMS), plus the external spool (index 254, "Ext").
  Colours can be overridden like on the U1.
* **Camera:** TLS socket on port 6000, 80-byte login, one JPEG frame per
  request (cached 2 s).
* **Light:** `system.ledctrl` on `chamber_light`. Pause, resume and cancel are
  MQTT print commands. There's no emergency stop over LAN; it stops the print.
* **Starting a print:** only sliced `.gcode.3mf` files. Same dialog and checks
  as the U1; the toolhead picker picks AMS trays. Upload by implicit FTPS (990)
  to the SD card, then `project_file` with `Metadata/plate_N.gcode`,
  `url=file:///sdcard/<name>`, `ams_mapping` (file filament → tray id, -1 for
  unused) and `use_ams` (off when everything goes to Ext). Bed leveling and
  vibration compensation on, timelapse and flow calibration off.
* **SD card cleanup:** only the newest `PP_PRINTER_KEEP_FILES` uploads from
  Print Pantry stay (ledger `uploads-p1s.json`).
* **History:** the P1S has no job list on LAN, so jobs are tracked from state
  changes into `jobs-p1s.json` and synced like Moonraker's.
* **Calibration:** bed leveling, vibration compensation and motor noise
  cancellation through the `calibration` command (option bits 2, 4, 8). The
  flow ratio test is sliced for filament 1 and printed from the chosen tray.

## Print history

Every print shows up on the **History** page and on its model's page, with when it ran, on which printer and
toolheads, how long it took, filament used and the outcome.

- The U1's Moonraker job history is read every few minutes (and right away when a print starts or ends), so prints
  started from the touchscreen or from a desktop slicer are logged too. The first sync imports all past jobs.
- A job is linked to a model by its file name; prints started from Print Pantry carry their file, model and toolhead map.
- When a print ends, the printer camera takes a picture of the result. You can add or replace the picture
  (phone upload or a new camera shot), change the outcome (for example mark a "completed" print as failed) and add
  a note by clicking the print. A status set by hand is kept even when the printer says otherwise.
- `grams` and `cost` are filled in by spool tracking through `history.on_finish(fn)` / `history.set_usage(...)`.
- Pictures are stored in the data volume under `history/`.
