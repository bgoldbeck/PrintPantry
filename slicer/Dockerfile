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

FROM ubuntu:24.04
ARG ORCA_VERSION
# The slicer binary links GTK/WebKit even in command-line mode, so the
# libraries must exist. No display is needed (and none is used).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 ca-certificates \
      libgtk-3-0t64 libwebkit2gtk-4.1-0 libopengl0 libglu1-mesa libegl1 libgl1 libgl1-mesa-dri \
      libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 libwayland-egl1 libdbus-1-3 libfontconfig1 libsm6 libice6 \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/man

COPY --from=extract /opt/orca /opt/orca
COPY slicer/*.py /srv/slicer/

ENV LC_ALL=C \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    SLICER_BIN=/opt/orca/AppRun \
    SLICER_PROFILES=/opt/orca/resources/profiles \
    SLICER_VERSION=${ORCA_VERSION} \
    PP_LIBRARY=/library \
    PP_CONFIG=/config \
    PP_SLICER_PORT=8485

USER 568:568
WORKDIR /srv/slicer
EXPOSE 8485
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8485/health',timeout=4)"
CMD ["python3", "/srv/slicer/service.py"]
