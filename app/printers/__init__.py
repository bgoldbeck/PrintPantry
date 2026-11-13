"""Printer control (v2): live status, camera snapshots, starting prints.

Printers are configured with environment variables so the compose file is the
one place to change addresses:

    PP_U1_URL          Moonraker on the Snapmaker U1, e.g. http://192.168.1.52:7125
    PP_U1_API_KEY      only if Moonraker on the U1 requires one (stock doesn't)
    PP_P1S_HOST / PP_P1S_SERIAL / PP_P1S_ACCESS_CODE   the P1S in LAN / Developer Mode
    PP_PRINTER_KEEP_FILES  how many files Print Pantry uploaded stay on a printer (default 5, 0 = keep all)
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .base import BUSY_STATES, IDLE_STATES, Printer, PrinterError, Status
from .bambu import BambuPrinter
from .moonraker import MoonrakerPrinter

__all__ = ["PrinterHub", "Printer", "PrinterError", "Status", "IDLE_STATES", "BUSY_STATES"]


class PrinterHub:
    def __init__(self, profiles, env: dict | None = None, drivers: dict | None = None, data: Path | None = None):
        env = os.environ if env is None else env
        self.printers: dict[str, Printer] = {}
        for p in profiles:
            if drivers and p.id in drivers:
                self.printers[p.id] = drivers[p.id]
            elif p.id == "u1":
                url = env.get("PP_U1_URL", "").strip()
                if url:
                    ledger = Path(data) / f"uploads-{p.id}.json" if data else None
                    self.printers[p.id] = MoonrakerPrinter(
                        p, url, env.get("PP_U1_API_KEY", ""), ledger=ledger,
                        keep_files=int(env.get("PP_PRINTER_KEEP_FILES", "5") or 0),
                    )
                else:
                    drv = Printer(p)
                    drv.configured = False
                    drv.setup_hint = "Set PP_U1_URL (for example http://192.168.1.52:7125) in the app's compose file."
                    drv.status = Status(state="not_set_up", message=drv.setup_hint)
                    self.printers[p.id] = drv
            elif p.id == "p1s":
                self.printers[p.id] = BambuPrinter(
                    p, env.get("PP_P1S_HOST", ""), env.get("PP_P1S_SERIAL", ""), env.get("PP_P1S_ACCESS_CODE", ""),
                    ledger=Path(data) / f"uploads-{p.id}.json" if data else None,
                    jobs_file=Path(data) / f"jobs-{p.id}.json" if data else None,
                    keep_files=int(env.get("PP_PRINTER_KEEP_FILES", "5") or 0),
                )

        # Held while a print or calibration is being sent, so two can't start at once.
        self.starting = {pid: threading.Lock() for pid in self.printers}
        if data:
            for drv in self.printers.values():
                drv.load_colors(Path(data) / f"colors-{drv.profile.id}.json")

    def get(self, printer_id: str) -> Printer | None:
        return self.printers.get(printer_id)

    def start(self) -> None:
        for p in self.printers.values():
            p.start()

    def stop(self) -> None:
        for p in self.printers.values():
            p.stop()
