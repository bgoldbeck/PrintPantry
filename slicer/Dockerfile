# Print Pantry slicer sidecar: OrcaSlicer run headless from the command line.
# It ships the Snapmaker U1 and Bambu P1S system profiles, so one slicer covers
# both printers. (Snapmaker's own Orca fork crashes when slicing a 3MF from the
# command line, so we use upstream OrcaSlicer.) The main app talks to it over HTTP (slicer/service.py).
#
# Built on TrueNAS by the custom app (deploy/truenas-compose.yaml). Expect an
# image of roughly 1 GB; the AppImage download is only needed during the build.

ARG ORCA_VERSION=2.4.2

FROM ubuntu:24.04 AS extract
ARG ORCA_VERSION
ARG ORCA_SHA256=d12fb8c8eac1aecd2dfb6377acd48f994f8fa439ed5292fa532dd82880f029fd
ADD --checksum=sha256:${ORCA_SHA256} \
    https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage \
    /tmp/orca.AppImage
WORKDIR /opt
RUN chmod +x /tmp/orca.AppImage \
 && /tmp/orca.AppImage --appimage-extract >/dev/null \
 && mv squashfs-root orca \
 # GUI-only parts the command line never touches (~70 MB).
 && rm -rf orca/resources/web orca/resources/i18n orca/resources/handy_models \
 && chmod -R a+rX orca

# Parametric models: FreeCAD's command line (freecadcmd) and OpenSCAD render
# .FCStd / .scad files to STL (slicer/render.py). Both come as AppImages,
# unpacked here; nothing needs a display.
FROM ubuntu:24.04 AS cad
ARG FREECAD_VERSION=1.1.4
ARG FREECAD_SHA256=f6dc6ba676e5ac96a565ebc8d657232f94c6158e85b4352141bd1a46f6b43434
ARG OPENSCAD_VERSION=2026.11-RC0
ARG OPENSCAD_SHA256=78c2050aedcf52cca0186151cdcda1118a771491e3bec934b3172d0b03a9de96
ADD --checksum=sha256:${FREECAD_SHA256} \
    https://github.com/FreeCAD/FreeCAD/releases/download/${FREECAD_VERSION}/FreeCAD_${FREECAD_VERSION}-Linux-x86_64-py311.AppImage \
    /tmp/freecad.AppImage
ADD --checksum=sha256:${OPENSCAD_SHA256} \
    https://github.com/openscad/openscad/releases/download/openscad-${OPENSCAD_VERSION}/OpenSCAD-${OPENSCAD_VERSION}-x86_64.AppImage \
    /tmp/openscad.AppImage
WORKDIR /opt
RUN chmod +x /tmp/*.AppImage \
 && /tmp/freecad.AppImage --appimage-extract >/dev/null && mv squashfs-root freecad \
 && /tmp/openscad.AppImage --appimage-extract >/dev/null && mv squashfs-root openscad \
 && rm -rf freecad/usr/share/doc freecad/usr/man openscad/usr/share/doc \
 && chmod -R a+rX freecad openscad

FROM ubuntu:24.04
ARG ORCA_VERSION
# The slicer binary links GTK/WebKit even in command-line mode, so the
# libraries must exist. No display is needed (and none is used).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 ca-certificates \
      libgtk-3-0t64 libwebkit2gtk-4.1-0 libopengl0 libglu1-mesa libegl1 libgl1 libgl1-mesa-dri \
      libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libwayland-egl1 libdbus-1-3 libfontconfig1 libsm6 libice6 \
      fonts-liberation fonts-dejavu-core fontconfig \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man

COPY --from=extract /opt/orca /opt/orca
COPY --from=cad /opt/freecad /opt/freecad
COPY --from=cad /opt/openscad /opt/openscad
COPY slicer/*.py slicer/fonts.conf /srv/slicer/
# freecadcmd needs the AppImage's environment (Python home, library path).
RUN printf '#!/bin/sh\nexec /opt/freecad/AppRun freecadcmd "$@"\n' > /usr/local/bin/freecadcmd \
 && chmod 755 /usr/local/bin/freecadcmd \
 # Smoke tests: a failure here is logged but doesn't stop slicing from working.
 && (HOME=/tmp QT_QPA_PLATFORM=offscreen /usr/local/bin/freecadcmd -c "import FreeCAD, Part, Mesh; print('FreeCAD', FreeCAD.Version()[:3])" \
     || echo "WARNING: FreeCAD smoke test failed") \
 && (echo 'cube(2);' > /tmp/t.scad && HOME=/tmp /opt/openscad/usr/bin/openscad -o /tmp/t.stl /tmp/t.scad \
     && rm -f /tmp/t.scad /tmp/t.stl || echo "WARNING: OpenSCAD smoke test failed") \
 && rm -rf /tmp/* /tmp/.[!.]* 2>/dev/null; true

ENV LC_ALL=C \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    SLICER_BIN=/opt/orca/AppRun \
    SLICER_PROFILES=/opt/orca/resources/profiles \
    SLICER_VERSION=${ORCA_VERSION} \
    FREECAD_BIN=/usr/local/bin/freecadcmd \
    OPENSCAD_BIN=/opt/openscad/usr/bin/openscad \
    PP_LIBRARY=/library \
    PP_CONFIG=/config \
    PP_SLICER_PORT=8485

USER 568:568
WORKDIR /srv/slicer
EXPOSE 8485
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8485/health',timeout=4)"
CMD ["python3", "/srv/slicer/service.py"]
