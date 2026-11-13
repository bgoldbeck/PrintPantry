# Calibration

The **Printers** page has a **Calibrate** row on each connected printer and a
**Filament flow ratio** panel below the printer cards.

## Filament flow ratio

1. Press **Flow ratio** on a printer (or **New flow test**). Pick the toolhead
   (T1–T4 on the U1), the filament preset, process, bed plate and the test:
   - **Standard**: 11 blocks from 0.95× to 1.05× the current flow, 1 % apart. Start here.
   - **Fine**: 16 blocks in 0.5 % steps, to fine-tune after the standard test.
   - **Coarse**: 9 blocks from 0.80× to 1.20×, for a filament that is far off.
   The test starts from your saved ratio for that filament if there is one,
   otherwise from the preset's own `filament_flow_ratio`.
2. The slicer on the NAS slices OrcaSlicer's own flow test plate
   (`resources/calib/filament_flow`), giving each block its own
   `print_flow_ratio` plus the per-object settings OrcaSlicer's Calibration
   menu uses (monotonic top surface, 5 top layers, speeds kept within the
   filament's volumetric limit). The file is kept in the library's hidden
   `.calibration/<printer>/` folder (newest 12).
3. The print dialog shows the camera, compares the loaded filament with the
   test's, and needs the "bed is clear" tick, like any print.
4. When it has printed, press **Pick best block** and choose the block with
   the smoothest top. The new ratio is `old ratio × block ratio`; it is saved
   per printer and filament preset in `calibration.json` in the data volume.

Saved ratios are used automatically by the **Slice** dialog whenever it slices
with presets (filaments with a saved ratio show `· flow 0.97 ✓`). Slicing with
"the project's own settings" uses what is in the project. **Preset** downloads
a filament preset that inherits the original and only changes the flow ratio,
for importing into OrcaSlicer or Snapmaker Orca (File > Import > Import Configs).
**Edit** sets a ratio by hand; the previous values are kept as history.

On the U1, a test for T2–T4 needs two runs of the slicer: one exports a
project with four filaments, the second slices it after moving the blocks to
the chosen toolhead (the command line crashes if objects use filament 2+ in a
project without per-filament settings).

## The printer's own calibrations (Snapmaker U1)

Checked against the U1's Klipper config (stock firmware, Nov 2026). Each needs
a tick that the bed is clear, runs in the background and shows on the card;
**Emergency stop** stops motors and heaters and restarts the firmware.

| Button | G-code sent |
| --- | --- |
| Bed mesh | `G28`, `AUTO_BED_MESH_CALIBRATE TEMP=<bed>` (heats, 2 min soak, 13×13 probe), `BED_MESH_PROFILE SAVE=default`, `SAVE_CONFIG RESTART=0` |
| Input shaper | `G28`, `SHAPER_CALIBRATE` (the U1's macro saves the result) |
| Toolhead offsets | `_EXTRUDER_OFFSET_ACTION_PREHEAT` (nozzles 140 °C), `G28`, `XYZ_OFFSET_CALIBRATE_ALL`, `_EXTRUDER_OFFSET_ACTION_EXIT` |

Pressure advance isn't a button: the U1 measures it per toolhead at the start
of every print (`SM_PRINT_FLOW_CALIBRATE` in its start G-code). PID tuning is
switched off in the U1's config (`allow_pid_calibrate = False`).

## Bambu P1S (later)

The flow test already slices for the P1S (as a `.gcode.3mf`); printing it waits
for P1S printer control. Its built-in calibrations (bed levelling, vibration
compensation, flow dynamics) can be started over MQTT once Developer Mode is
on; they will appear in the same Calibrate row.
