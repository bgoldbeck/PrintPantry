# Installing Print Pantry on TrueNAS SCALE 25.10

This puts the library on **testpool** for now. Everything lives under one dataset,
`testpool/printpantry`, so moving to another pool later means changing one path
(see [Moving to another pool](#moving-to-another-pool)).

## Quick way: scripts from your PC (recommended)

The NAS builds the image itself from GitHub, so you need neither Docker Desktop
nor a registry. From the repo folder on Windows (uses the `sparkfall-truenas`
SSH alias from `~/.ssh/config`):

```
scripts\deploy-truenas.cmd setup   first time: datasets, code checkout, app install
scripts\sync-library.cmd           copy D:\PrintPantry\library to the NAS
scripts\sync-config.cmd            copy D:\PrintPantry\config (slicer presets) to the NAS
scripts\deploy-truenas.cmd         later: pull latest main, rebuild, restart
```

`setup` creates `testpool/printpantry/{library,data,src,config}`, sets permissions
(library: you + apps group; data: apps user 568), clones the repo into `src/repo`
and installs a custom app named **printpantry** from
[deploy/truenas-compose.yaml](../deploy/truenas-compose.yaml). It talks to
TrueNAS through `midclt`, which the admin user can run without sudo. Then open
<http://192.168.1.140:8484>.

Settings live in `scripts\_config.cmd` (`PP_SSH_HOST`, `PP_NAS_ROOT`,
`PP_LOCAL_LIBRARY`).

**Login:** the deploy script makes a random password the first time and keeps
it only on the NAS (`src/.pp_password`), never in this public repo. A login
lasts 365 days (`PP_SESSION_DAYS`).

```
scripts\deploy-truenas.cmd password              show it
scripts\deploy-truenas.cmd password my-new-pass  change it (restarts the app)
```

## Manual way: TrueNAS UI and a published image

Use this if you publish the image to ghcr.io with `scripts\release.cmd`
(needs a working Docker Desktop).

### 1. Create the datasets

**Datasets** > select `testpool` > **Add Dataset**:

| Dataset | Preset | Purpose |
| --- | --- | --- |
| `testpool/printpantry` | Generic | parent |
| `testpool/printpantry/library` | SMB | your models; you copy files here from your PC |
| `testpool/printpantry/data` | Apps | the app's database and preview pictures (a few MB) |

### 2. Permissions

The container runs as the TrueNAS **apps** user (uid/gid 568).

* `library`: **Edit ACL** > add an entry for user `apps` with **Read** and
  **Traverse** (apply recursively). Your own SMB user keeps full control.
* `data`: **Edit Permissions** > owner user `apps`, group `apps`, apply.

### 3. Share the library with your PC

**Shares** > **SMB** > **Add** > path `/mnt/testpool/printpantry/library`, name
`printpantry`. On Windows, map `\\192.168.1.140\printpantry` as a drive and copy
the contents of `D:\PrintPantry\library` into it.

Folder layout the app expects:

```
library/
  Tools/                       <- category tile (any folder name)
    Wrench Holder/             <- one model
      wrench_holder.stl        <- shown in the 3D viewer
      wrench_holder.3mf        <- project 3MF (editable source)
      wrench_holder_P1S.gcode.3mf   <- sliced for the P1S
      wrench_holder_U1.gcode        <- sliced for the U1
      images/photo.jpg
```

### 4. Install the app

**Apps** > **Discover Apps** > the three-dot menu > **Install via YAML**. Name it
`printpantry` and paste:

```yaml
services:
  printpantry:
    image: ghcr.io/bgoldbeck/printpantry:latest
    restart: unless-stopped
    user: "568:568"
    ports:
      - "8484:8484"
    environment:
      PP_PASSWORD: "pick-a-password"
      PP_SCAN_INTERVAL: "300"
      TZ: "America/Los_Angeles"
    volumes:
      - /mnt/testpool/printpantry/library:/library:ro
      - /mnt/testpool/printpantry/data:/data
    deploy:
      resources:
        limits:
          memory: 512M
```

Open <http://192.168.1.140:8484>. New files show up within 5 minutes, or press
**Rescan**.

Before the first install, run `scripts\release.cmd` and make the ghcr package
public (GitHub > Packages > printpantry > Package settings). To update later:
run `scripts\release.cmd`, then on TrueNAS
**Apps** > printpantry > **Update** (or Stop/Start, which pulls `latest` again).

## Disk use: what cleans itself up

Print Pantry never deletes anything in your library or presets folders, except
that slicing replaces its own earlier output for the same file. What it keeps
in check:

- **Old images.** Each deploy rebuilds both images; the previous ones (about
  1 GB for the slicer) are deleted after the new version is up. Only images
  `truenas.sh` recorded as Print Pantry's are touched. `deploy-truenas.cmd prune`
  runs the same cleanup by hand.
- **Container logs.** Capped at 3 x 10 MB per container in the compose file.
- **Slicer scratch files.** Each job works in a temporary folder that is deleted
  when the job ends, and leftovers from a crash are swept when the slicer starts.
  Job logs in memory are capped at 20,000 characters each, 50 jobs.
- **Sliced files.** Re-slicing writes over the same `sliced/<name>_U1.gcode` /
  `_P1S.gcode.3mf`. A full re-slice also removes that file's outdated per-plate
  G-code (e.g. a plate 3 that no longer exists).
- **STL previews** in `data/thumbs` are deleted when their STL is removed or changes.
- **Files on the U1.** Print Pantry remembers what it uploaded (`data/uploads-u1.json`)
  and keeps only its newest 5 on the printer (`PP_PRINTER_KEEP_FILES`, 0 = keep
  all). Files you put on the printer another way are left alone.

Not cleaned automatically: Docker's build cache (it is reused by every build,
so it stays roughly the size of one slicer build).

## Moving to another pool

1. Stop the app.
2. Copy the dataset, e.g. **Data Protection** > replication, or from a shell:
   `zfs snapshot -r testpool/printpantry@move && zfs send -R testpool/printpantry@move | zfs recv tank/printpantry`
3. Set `PP_NAS_ROOT=/mnt/tank/printpantry` in `scripts\_config.cmd` and run
   `scripts\deploy-truenas.cmd` (or, for the manual install, replace
   `/mnt/testpool/` with `/mnt/tank/` in the app's YAML). Start it again. Category icons and previews come along in `data`.

## Notes

* The library is mounted **read-only**; the app never changes your files.
* Memory use is small (about 40 MB at idle). STL previews for cards are rendered
  by your browser once and stored in `data/thumbs`.
