"""Plain-language explanations for printer errors.

The Snapmaker U1's Klipper puts its last error in ``print_stats.exception`` as
``{id, index, code, message, level}``; ``index`` is the toolhead (0 = T1) for
toolhead errors. The codes below come from the U1's own Klipper source
(Snapmaker/u1-klipper, checked Nov 2026): ``id`` 523 is a toolhead/filament
problem, 531 a print-job problem, 522/526/527 the mainboard or heaters, 529 a
bad G-code command, 530 bed levelling.

A problem is a dict the UI can show as is:
``{title, detail, fix: [steps], head: int | None, code, raw, action}``, where
``action`` names a button the app can offer ("set_filament").
"""

from __future__ import annotations

import re

# (id, code) -> (title, detail, fix steps, action). "{t}" is the toolhead name (T1..T4).
U1_CODES: dict[tuple[int, int], tuple[str, str, list[str], str]] = {
    (523, 0): (
        "{t} ran out of filament",
        "The filament sensor on {t} stopped seeing filament, so the printer paused.",
        ["Load a new spool on {t} (Filament page on the printer, or feed it by hand).",
         "Check the filament type and colour for {t} are set.",
         "Press Resume."],
        "",
    ),
    (523, 38): (
        "{t} filament looks tangled",
        "The tangle sensor on {t} saw the nozzle pull filament but the spool didn't turn, "
        "so it paused. Usually the spool is snagged, crossed over itself, or the spool holder is stuck.",
        ["Check the {t} spool turns freely and the filament isn't crossed or caught.",
         "Pull a little filament off the spool by hand so it feeds freely.",
         "Make sure {t} has a filament type set (the U1 won't resume without one).",
         "Press Resume. If it keeps stopping with nothing wrong, lower the tangle sensitivity in Settings."],
        "",
    ),
    (523, 39): (
        "{t} has no filament type set",
        "This print uses {t}, but the printer doesn't know what filament is in it "
        "(no RFID tag and none picked on the screen). The U1 refuses to start or resume until it's set.",
        ["Set the filament for {t} with the button below (or on the printer's Filament page).",
         "Press Resume."],
        "set_filament",
    ),
    (523, 48): (
        "Toolhead contacts lost connection",
        "The printer saw the pogo-pin contacts between a toolhead and the carriage drop out during the print.",
        ["Wipe the toolhead and carriage contacts clean.",
         "Make sure each toolhead is seated properly.",
         "Press Resume, or cancel and restart the print if it won't continue."],
        "",
    ),
    (523, 2): (
        "{t} heater problem",
        "The printer lost contact with the heater or temperature sensor on {t}.",
        ["Check {t} is seated and its contacts are clean.", "Restart the printer if the error stays."],
        "",
    ),
    (523, 3): (
        "{t} isn't heating properly",
        "{t}'s temperature didn't follow its heater (heater or sensor fault, or a draft/blob on the nozzle).",
        ["Check {t} for a filament blob around the nozzle and its sock.", "Restart the printer, then try again."],
        "",
    ),
    (523, 9000): ("Flow calibration failed", "Flow calibration stopped with an error.",
                  ["Check the filament on the toolhead it was calibrating, then try again."], ""),
    (523, 9001): ("Flow calibration not allowed", "The printer won't run flow calibration right now.",
                  ["Make sure the printer is idle and the filament is set, then try again."], ""),
    (531, 10): ("Can't read the print file", "The printer couldn't read the G-code file.",
                ["Send the print again from Print Pantry."], ""),
    (531, 21): ("Filament is still loading", "Auto feeding is in progress, so the print can't start yet.",
                ["Wait for loading to finish on the printer, then start again."], ""),
    (531, 26): ("Nothing to resume", "The printer isn't paused, so there's nothing to resume.", [], ""),
    (531, 27): ("Nothing to pause", "The printer isn't printing, so it can't pause.", [], ""),
    (530, 29): ("Bed levelling failed", "Probing the bed for the mesh failed.",
                ["Clear the bed and clean the build plate.", "Run Bed mesh calibration again."], ""),
    (522, 14): ("Mainboard error", "The printer's mainboard reported a fault.",
                ["Restart the printer from its screen or power switch."], ""),
    (522, 15): ("Heater safety stop", "A heater didn't behave as expected and the printer shut it off.",
                ["Let it cool, check for blobs on the nozzles, then restart the printer."], ""),
    (526, 0): ("Mainboard stopped responding", "Klipper lost contact with the mainboard.",
               ["Restart the printer."], ""),
    (526, 1): ("Bed heater problem", "The bed's temperature didn't follow its heater.",
               ["Restart the printer, then try again."], ""),
    (527, 0): ("Toolhead board stopped responding", "Klipper lost contact with a toolhead board.",
               ["Check the toolheads are seated, then restart the printer."], ""),
}
LEVELS = {1: "warning", 2: "error", 3: "error"}

NOT_EDITED = re.compile(r"\be(\d)\s+not\s+edit\s+filament", re.I)
RUNOUT = re.compile(r"\be(\d)_filament\s+runout", re.I)
TANGLED = re.compile(r"extruder\[(\d)\].*tangled|tangled", re.I)


def head_name(index) -> str:
    return f"T{int(index) + 1}" if isinstance(index, int) and index >= 0 else "a toolhead"


def _fill(text: str, t: str) -> str:
    return text.replace("{t}", t)


def problem(code_id: int, code: int, index, raw: str, level: int | None = None) -> dict:
    t = head_name(index)
    title, detail, fix, action = U1_CODES.get((code_id, code), ("", "", [], ""))
    if not title:
        title = raw.strip().rstrip("!").capitalize() or "Printer error"
        detail = "The printer reported an error Print Pantry doesn't have an explanation for yet."
        fix = ["Check the printer's screen for details."]
    head = int(index) if isinstance(index, int) and index >= 0 and code_id == 523 else None
    if (code_id, code) == (523, 39) and head is None:
        title = title.replace("{t}", "A toolhead")
    return {
        "title": _fill(title, t),
        "detail": _fill(detail, t),
        "fix": [_fill(f, t) for f in fix],
        "head": head,
        "code": f"{code_id}-{code}",
        "raw": raw,
        "level": LEVELS.get(level or 2, "error"),
        "action": action,
    }


def u1_exception(exc) -> dict | None:
    """``print_stats.exception`` (or one entry of ``exception_manager.exceptions``) to a problem."""
    if not isinstance(exc, dict) or not exc:
        return None
    try:
        code_id, code = int(exc.get("id")), int(exc.get("code"))
    except (TypeError, ValueError):
        return None
    index = exc.get("index")
    index = int(index) if isinstance(index, (int, float)) else None
    return problem(code_id, code, index, str(exc.get("message") or ""), exc.get("level"))


def u1_text(text: str) -> dict | None:
    """Recognise a U1 error message (e.g. a refused RESUME) without a code."""
    if not text:
        return None
    if m := NOT_EDITED.search(text):
        return problem(523, 39, int(m.group(1)), text)
    if m := RUNOUT.search(text):
        return problem(523, 0, int(m.group(1)), text)
    if TANGLED.search(text):
        m = re.search(r"\[(\d)\]", text)
        return problem(523, 38, int(m.group(1)) if m else None, text)
    return None


def unset_heads_blocker(heads: list[int]) -> dict:
    """The print uses toolheads with no filament type, which blocks resume."""
    names = ", ".join(head_name(i) for i in heads)
    p = problem(523, 39, heads[0], f"e{heads[0]} not edit filament")
    if len(heads) > 1:
        p["title"] = f"{names} have no filament type set"
        p["detail"] = (f"This print uses {names}, but the printer doesn't know what filament is in them. "
                       "The U1 won't resume until each one is set.")
        p["fix"] = [f"Set the filament for {names} with the button below (or on the printer's Filament page).",
                    "Press Resume."]
    p["heads"] = heads
    return p
