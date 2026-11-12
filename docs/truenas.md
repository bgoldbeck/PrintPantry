# Installing Print Pantry on TrueNAS SCALE 25.10

This puts the library on **testpool** for now. Everything lives under one dataset,
`testpool/printpantry`, so moving to another pool later means changing one path
(see [Moving to another pool](#moving-to-another-pool)).

## 0. Build and upload the image (on your Windows PC)

From the repo folder, with Docker Desktop running:

```
scripts\login.cmd      (first time only: GitHub token with write:packages)
scripts\release.cmd    (tests, builds and pushes ghcr.io/bgoldbeck/printpantry)
```

The first push creates the package as **private**. Make it public once so
TrueNAS can pull without a login: GitHub > your profile > **Packages** >
printpantry > **Package settings** > Change visibility > Public.

## 1. Create the datasets

**Datasets** > select `testpool` > **Add Dataset**:

| Dataset | Preset | Purpose |
| --- | --- | --- |
| `testpool/printpantry` | Generic | parent |
| `testpool/printpantry/library` | SMB | your models; you copy files here from your PC |
| `testpool/printpantry/data` | Apps | the app's database and preview pictures (a few MB) |

## 2. Permissions

The container runs as the TrueNAS **apps** user (uid/gid 568).

* `library`: **Edit ACL** > add an entry for user `apps` with **Read** and
  **Traverse** (apply recursively). Your own SMB user keeps full control.
* `data`: **Edit Permissions** > owner user `apps`, group `apps`, apply.

## 3. Share the library with your PC

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

## 4. Install the app

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

To update later: run `scripts\release.cmd` on your PC, then on TrueNAS
**Apps** > printpantry > **Update** (or Stop/Start, which pulls `latest` again).

## Moving to another pool

1. Stop the app.
2. Copy the dataset, e.g. **Data Protection** > replication, or from a shell:
   `zfs snapshot -r testpool/printpantry@move && zfs send -R testpool/printpantry@move | zfs recv tank/printpantry`
3. Edit the app's YAML and replace `/mnt/testpool/` with `/mnt/tank/` in the two
   volume lines. Start it again. Category icons and previews come along in `data`.

## Notes

* The library is mounted **read-only**; the app never changes your files.
* Memory use is small (about 40 MB at idle). STL previews for cards are rendered
  by your browser once and stored in `data/thumbs`.
