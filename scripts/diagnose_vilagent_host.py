"""Diagnose VILAGENT dedicated Windows host startup.

Run from the repository root with:
    python scripts/diagnose_vilagent_host.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "backend" / "packages" / "harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from vilagent.computer_use.windows.child_process import DedicatedWindowsHostProcess
from vilagent.config.app_config import AppConfig


async def main() -> None:
    config = AppConfig.from_file(str(ROOT / "config.yaml")).computer_use
    process = DedicatedWindowsHostProcess(config)
    process.start()
    try:
        handshake = await asyncio.to_thread(process.wait_handshake, 20)
        print(handshake.model_dump(mode="json"))
        if handshake.ready:
            heartbeat = await process.create_client().heartbeat()
            print({"heartbeat": heartbeat.model_dump(mode="json")})
    finally:
        try:
            process.request_stop()
        except Exception:
            pass
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            process.join(5)


if __name__ == "__main__":
    asyncio.run(main())
