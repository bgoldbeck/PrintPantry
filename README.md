# Print Pantry

A self-hosted library for 3D printing files. Point it at a folder and it shows
big category tiles, a spinnable 3D view of each STL, and every sliced print file
grouped under the model it belongs to, with plates, print time, filament and
thumbnails.

Built for a home setup with a **Bambu Lab P1S** and a **Snapmaker U1**, running
as a container on **TrueNAS SCALE**.

![Print Pantry home](docs/screenshots/home.png)

## What v1 does

* Scans a library folder (read-only). One top-level folder per category, one
  folder per model inside it. Files directly inside a category folder are
  grouped into models by file name (`planter.stl`, `planter_P1S.gcode.3mf`,
  `planter_U1.gcode` become one model). Folders that only sort by file type
  (`stls`, `3mf`, `gcode`, `files`, `prints`...) are looked through, so
  `library/stls/clip.stl` and `library/3mf/clip.3mf` are one model.
* Category tiles with icons picked from the name (Tools gets a wrench, Garden a
  sprout...). Change the icon and colour in the UI, or drop a
  `_category.json` like `{"icon": "hammer", "color": "#E4572E"}` in the folder.
* Model pages with a Three.js STL viewer (drag to spin, scroll to zoom), size in
  mm, and a switcher when a model has several STLs.
* Sliced files are detected and tagged by printer:
  * P1S `.gcode.3mf`: every plate with its thumbnail, time, weight and
    filament colours, read from `slice_info.config`.
  * U1 `.gcode` (Snapmaker Orca): thumbnail, time, per-toolhead filament.
* Every STL is viewable, with or without a project 3MF. Each STL lists all the
  project 3MFs made from it (any number): a 3MF belongs to an STL when the STL
  is named inside it, or when its file name starts with the STL's name
  (`clip.3mf`, `clip x4.3mf`). With one STL in a model, every 3MF belongs to it.
* **Create New Source** opens the STL in your desktop slicer so you can make a
  new project 3MF; **Open in ...** does the same for an existing 3MF. Pick
  Snapmaker Orca or Bambu Studio in the "Open in" menu; the choice is saved on
  the server. See [Opening files in a desktop slicer](#opening-files-in-a-desktop-slicer).
* Search across model names, tags, categories, file names and the parts named
  inside 3MFs; name hits come first. `tag:kitchen` and `cat:tools` narrow it
  down (use `_` for spaces in a tag). Optional password login.
* **Tags** on every model: add or remove them on the model page. The home page
  lists all tags; click one to see its models. Tags live in the app's
  database (keyed by the model's folder), so the library itself isn't touched.
* **Import** a model from a Printables or MakerWorld link. It downloads into
  `library/<Category>/<Model name>/` with pictures in `images/`, a
  `pantry-source.json` saying where it came from, and the site's tags. The
  category is guessed from the site's own category when one of yours matches.
  * Printables: STL and 3MF files, pictures, tags, author, licence. No login.
  * MakerWorld: details, tags and pictures work without login, but MakerWorld
    only gives files to logged-in accounts. Paste your MakerWorld `token`
    cookie under Import > MakerWorld login (stored only on the NAS) and the
    3MF comes along too. Without it, drop the 3MF into the folder yourself.
  * Imported files exist only on the NAS (scripts\sync-library.cmd copies PC
    to NAS, not back). The app's library mount is writable for this.

![Model page](docs/screenshots/model.png)

## Install on TrueNAS

From Windows, with the `sparkfall-truenas` SSH alias set up:

```
scripts\deploy-truenas.cmd setup    datasets + checkout + app, built on the NAS
scripts\sync-library.cmd            copy D:\PrintPantry\library over
scripts\deploy-truenas.cmd          update to the latest main later
```

The UI is on port 8484. Details and the manual (TrueNAS UI) route are in
[docs/truenas.md](docs/truenas.md).

## Publishing an image to ghcr.io (optional)

Not needed for the TrueNAS deploy above, which builds on the NAS. There is no
CI; to publish an image by hand (needs Docker Desktop):

| Script | Does |
| --- | --- |
| `scripts\login.cmd` | one-time `docker login ghcr.io` (token with `write:packages`) |
| `scripts\test.cmd` | runs the tests in a throwaway Python container |
| `scripts\build.cmd` | builds `ghcr.io/bgoldbeck/printpantry:<version>` and `:latest` for linux/amd64 |
| `scripts\push.cmd` | uploads both tags |
| `scripts\release.cmd` | test + build + push (`release.cmd notest` skips tests) |

The version comes from `app/__init__.py`; bump it before a release.

## Opening files in a desktop slicer

A web page can't start programs, so Print Pantry gives the slicer a short-lived
download link (15 minutes, no login needed for that one file) through the
slicer's link handler:

* **Snapmaker Orca** works with nothing to install: its `snapmaker-orca://`
  link downloads the file (into your Downloads folder) and opens it.
* **Bambu Studio** only accepts `bambustudio://` links from MakerWorld, so on
  each Windows PC run this once from the repo checkout:

      scripts\install-slicer-links.cmd

  It registers a `printpantry://` link for your Windows user that downloads the
  file to `%TEMP%\PrintPantry` and starts Bambu Studio with it. It only follows
  links to `192.168.1.140:8484` (pass another `host:port` to add one;
  `remove` undoes it).

The first click in each browser asks whether to open the app; tick "Always
allow".

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PP_LIBRARY` | `/library` | folder to scan |
| `PP_DATA` | `/data` | database and preview cache |
| `PP_PASSWORD` | empty | set to require a login |
| `PP_SCAN_INTERVAL` | `300` | seconds between rescans (0 = startup and button only) |
| `PP_PORT` | `8484` | HTTP port |
| `PP_U1_URL` | empty | Moonraker on the Snapmaker U1, e.g. `http://192.168.1.52:7125` |
| `PP_U1_API_KEY` | empty | only if the U1's Moonraker requires a key (stock doesn't) |
| `PP_SLICER_URL` | empty | the slicer container, e.g. `http://slicer:8485`; empty hides the Slice buttons |
| `PP_PRINTER_KEEP_FILES` | `5` | how many files Print Pantry uploaded stay on the U1; older ones are deleted (0 = keep all) |

## Development

```sh
pip install -r requirements.txt -r requirements-dev.txt
python scripts/make_demo_library.py demo-library      # fake STLs, 3MFs, G-code
PP_LIBRARY=demo-library PP_DATA=data python -m app.main
pytest -q
```

Layout:

```
app/formats.py   3MF / G-code readers (plates, thumbnails, printer model)
app/scanner.py   folder walk, grouping into categories and models
app/config.py    env settings and printer profiles
app/store.py     SQLite: parse cache, category settings
app/main.py      FastAPI routes
app/printers/    printer drivers (Moonraker for the U1, Bambu stub) and /api/printers
app/slicing.py   /api/slicer, which forwards to the slicer container
slicer/          the slicer container: OrcaSlicer CLI + a stdlib HTTP service
app/static/      single-page UI, Three.js viewer (vendored, no build step)
```

## Roadmap

* **v2 printers** (U1 done, see [docs/printers.md](docs/printers.md)): live
  status, camera snapshots, pause/resume/cancel, and "Print on U1" with a
  bed-clear confirm and a toolhead filament check. The P1S follows once
  Developer Mode is on.
* **v3 slicing** (done, see [docs/slicing.md](docs/slicing.md)): "Slice for U1"
  and "Slice for P1S" on project 3MFs and STLs, run by OrcaSlicer's command
  line in a second container on the NAS, with built-in or your own presets.
* **Calibration** (U1 done, see [docs/calibration.md](docs/calibration.md)):
  a filament flow ratio wizard (OrcaSlicer's flow test plates, sliced on the
  NAS, saved per filament and used when slicing) and buttons for the U1's bed
  mesh, input shaper and toolhead offset calibrations.

Three.js (MIT) and Lucide icons (ISC) are vendored under `app/static/vendor`.
