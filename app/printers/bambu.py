"""Bambu Lab P1S: placeholder until Developer Mode is switched on.

The plan (see docs/printers.md): status over MQTT/TLS port 8883 (user ``bblp``,
password = access code, topic ``device/<serial>/report``), prints by FTPS
upload to the SD card then an MQTT ``project_file`` command, camera from the
JPEG stream on port 6000. The driver below only reports that it isn't set up,
so the Printers page can already show the P1S card.
"""

from __future__ import annotations

from .base import Printer, Status


class BambuPrinter(Printer):
    def __init__(self, profile, host: str = "", serial: str = "", access_code: str = ""):
        super().__init__(profile)
        self.host, self.serial, self.access_code = host, serial, access_code
        self.configured = False
        self.setup_hint = (
            "Not connected yet. Turn on LAN-only and Developer Mode on the P1S; "
            "printer control for it is the next step after the U1."
        )
        self.status = Status(online=False, state="not_set_up", message=self.setup_hint)
