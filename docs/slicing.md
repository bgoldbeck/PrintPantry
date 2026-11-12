# Slicing on the NAS

Print Pantry can slice for you: open a model, then press **Slice for Snapmaker
U1** (or **Slice for Bambu P1S**) next to a project 3MF, or next to the STL
when the model has no project. The new file is saved next to the source in a
`sliced` folder (for example `Tools/Wrench Holder/sliced/wrench_holder_U1.gcode`)
and shows up under **Print files**, ready for **Print on Snapmaker U1**.
Slicing again replaces the earlier file of the same name.

## How it works

A second container, `slicer`, runs [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer)
2.4.2 headless from its command line, behind a tiny HTTP API
([slicer/service.py](../slicer/service.py)). Only the main app talks to it.
OrcaSlicer ships the Snapmaker U1 and Bambu P1S profiles, so one slicer covers
both printers. (Snapmaker's own Orca fork crashes when it slices a 3MF from
the command line, so upstream OrcaSlicer is used.)

- One slice runs at a time; the container is limited to 2 GB of memory.
- The image is about 1.4 GB, built on the NAS the first time you deploy
  (it downloads the OrcaSlicer AppImage, checked against a fixed SHA-256).
- Plate thumbnails can't be drawn without a screen, so sliced files have none.
  The U1 still prints them; it just shows no picture.

## Settings

The dialog offers two ways:

- **Use the project's own settings** (only when the 3MF was set up for that
  printer): slices exactly what the project says.
- **Pick presets**: printer, process, bed plate and one filament per slot.
  Use this to slice a P1S project for the U1. The objects, plates, painting
  and filament slots stay as they are in the project; the presets replace the
  printer, process and filament settings.

The U1 has 4 toolheads, so projects with more than 4 filaments can't be
sliced for it. The dialog warns when a filament has no bed temperature for the
chosen plate (OrcaSlicer's "Snapmaker PLA @U1", for example, only defines one
for the smooth PEI / High Temp plate).

### Your own presets

Built-in presets need nothing. To use your own printer or process presets,
export them (File > Export > Export Preset Bundle, or Export Configs) into
`D:\PrintPantry\config` and run:

```
scripts\sync-config.cmd
```

That copies the folder to `testpool/printpantry/config` on the NAS. The slicer
reads loose `.json` presets and `.orca_printer`, `.bbscfg`, `.orca_filament`
and `.bbsflmt` bundles, resolves what they inherit from the built-in profiles,
and lists them first, marked "(yours)". Presets made in Snapmaker Orca work
too, even though it names some built-in processes slightly differently
(`0.20mm Standard` vs `0.20 Standard`).

## Troubleshooting

- **No Slice buttons**: the app can't reach the slicer. Check Apps >
  printpantry > Logs on TrueNAS for the `slicer` container.
- **"Slicer failed"**: open *Slicer log* in the dialog. OrcaSlicer sometimes
  crashes on a file that slices fine on the next try, so the service retries a
  crash twice before giving up.
- **Out of disk space**: the slicer image needs about 1.4 GB plus ~0.5 GB of
  build cache on testpool. `docker builder prune` on the NAS frees the cache.
