"""Crash-isolated bridge for the undocumented virtual-desktop COM interfaces.

pyvda's internal manager changes between Windows builds and an access violation
there would take the whole backend down. Every call that can touch those
interfaces runs in this tiny subprocess and reports JSON on stdout.

    python vd_worker.py list
    python vd_worker.py create "Compa workspace"
    python vd_worker.py remove <desktop-guid>
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "list"
    payload: dict[str, object]
    try:
        from pyvda import VirtualDesktop, get_virtual_desktops

        if action == "list":
            current = VirtualDesktop.current()
            payload = {
                "ok": True,
                "current": {"id": str(current.id), "number": current.number},
                "desktops": [
                    {"id": str(item.id), "number": item.number, "name": item.name}
                    for item in get_virtual_desktops()
                ],
            }
        elif action == "create":
            name = argv[2] if len(argv) > 2 else "Compa workspace"
            desktop = VirtualDesktop.create()
            try:
                desktop.rename(name)
            except Exception:  # noqa: BLE001 - naming is cosmetic
                pass
            payload = {
                "ok": True,
                "desktop": {
                    "id": str(desktop.id),
                    "number": desktop.number,
                    "name": desktop.name,
                },
            }
        elif action == "remove":
            target = str(argv[2])
            fallback = VirtualDesktop.current()
            removed = False
            for desktop in get_virtual_desktops():
                if str(desktop.id) == target:
                    desktop.remove(fallback)
                    removed = True
                    break
            payload = {"ok": removed, "removed": removed}
        else:
            payload = {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as exc:  # noqa: BLE001 - any COM failure is a capability gap
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
