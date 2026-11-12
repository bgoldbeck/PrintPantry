"""Printer control (v2): live status, camera snapshots, starting prints.

Printers are configured with environment variables so the compose file is the
one place to change addresses:

    PP_U1_URL          Moonraker on the Snapmaker U1, e.g. http://192.168.1.52:7125
    PP_U1_API_KEY      only if Moonraker on the U1 requires one (stock doesn't)
    PP_P1S_HOST / PP_P1S_SERIAL / PP_P1S_ACCESS_CODE   reserved for the P1S
"""

from __future__ import annotations

import os

from .base import BUSY_STATES, IDLE_STATES, Printer, PrinterError, Status
from .bambu import BambuPrinter
from .moonraker import MoonrakerPrinter

__all__ = ["PrinterHub", "Printer", "PrinterError", "Status", "IDLE_STATES", "BUSY_STATES"]


class PrinterHub:
    def __init__(self, profiles, env: dict | None = None, drivers: dict | None = None):
        env = os.environ if env is None else env
        self.printers: dict[str, Printer] = {}
        for p in profiles:
            if drivers and p.id in drivers:
                self.printers[p.id] = drivers[p.id]
            elif p.id == "u1":
                url = env.get("PP_U1_URL", "").strip()
                if url:
                    self.printers[p.id] = MoonrakerPrinter(p, url, env.get("PP_U1_API_KEY", ""))
                else:
                    drv = Printer(p)
                    drv.configured = False
                    drv.setup_hint = "Set PP_U1_URL (for example http://192.168.1.52:7125) in the app's compose file."
                    drv.status = Status(state="not_set_up", message=drv.setup_hint)
                    self.printers[p.id] = drv
            elif p.id == "p1s":
                self.printers[p.id] = BambuPrinter(
                    p, env.get("PP_P1S_HOST", ""), env.get("PP_P1S_SERIAL", ""), env.get("PP_P1S_ACCESS_CODE", "")
                )

    def get(self, printer_id: str) -> Printer | None:
        return self.printers.get(printer_id)

    def start(self) -> None:
        for p in self.printers.values():
            p.start()

    def stop(self) -> None:
        for p in self.printers.values():
            p.stop()
