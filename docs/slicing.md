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

### A colour per object

When the file has more than one piece, the dialog adds a **Colours** section
with a 3D preview. Each piece gets its own toolhead (T1-T4 on the U1): pick
it in the list, or choose a toolhead button and click pieces in the preview.
Pieces show in the colour loaded on that toolhead, and the filament rows grow
to match the highest toolhead used, with presets matching what's loaded.

- **Project 3MF**: one row per object, or per part when an object is made of
  several parts (a multi-colour model). The file's own choices are the
  starting point; only what you change is written into the copy that gets
  sliced (`Metadata/model_settings.config`), never into your file. Painted
  colours stay as painted; the toolhead applies to the unpainted rest, and a
  painted project can only use the filaments it already has. Moving objects
  onto more toolheads than the project has filaments slices its meshes with
  your chosen presets only (OrcaSlicer can't add filaments to a project), so
  per-object settings and modifiers from the project are left out.
- **STL with separate bodies** (e.g. a sign whose letters are separate
  pieces): each body gets a row, starting on different toolheads.
- **Several STLs in one model** (colour sets as downloaded from model sites):
  tick *Slice together with* to add them. *Keep their positions* (on when the
  STLs touch or overlap) keeps them where they were designed to fit;
  untick it for separate pieces and the slicer lays them out on the bed.

STLs are combined into a 3MF for the slicer (one part per body, filament per
part). STLs over 600,000 triangles aren't split into bodies.

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
