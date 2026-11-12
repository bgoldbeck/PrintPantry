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
* **Controls:** pause, resume, cancel (with a confirm).

## Bambu P1S (next)

Shown as "Not set up" until Developer Mode is on. Plan: MQTT/TLS on 8883
(`bblp` / access code) for status and the `project_file` print command, FTPS
upload to the SD card, JPEG camera stream on port 6000, and an AMS slot
mapping step in the same print dialog. Settings will be `PP_P1S_HOST`,
`PP_P1S_SERIAL`, `PP_P1S_ACCESS_CODE` (`app/printers/bambu.py`).
