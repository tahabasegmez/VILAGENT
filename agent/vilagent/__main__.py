"""Start the VILAGENT gateway: ``python -m vilagent --port 8001 --data-dir <dir> [--static-dir <ui>]``.

The desktop launcher (desktop/electron/main.cjs) runs this; it can also be run by hand.
Everything the process writes (config, state, logs, runtime files) lives in the data
dir, which becomes the working directory before the app is imported.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import shutil
import sys
from pathlib import Path


def loop_kwargs() -> dict[str, str]:
    """On Windows, let asyncio pick its own loop (Proactor) instead of uvicorn's choice.

    With ``--reload`` uvicorn runs the server on a Selector loop, and that loop cannot spawn
    subprocesses on Windows. Playwright starts its driver as one, so every browser step in a
    development run failed with a bare ``NotImplementedError``. ``loop="none"`` leaves the loop
    to ``asyncio.run``, whose Windows default (Proactor) can spawn processes.
    """
    return {"loop": "none"} if sys.platform == "win32" else {}


def _resource_dir() -> Path:
    """Where bundled templates live: the PyInstaller bundle, or the repository root."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def _default_data_dir() -> Path:
    appdata = os.getenv("APPDATA")
    return Path(appdata) / "VILAGENT" if appdata else Path.home() / ".vilagent"


def _bootstrap_data_dir(data_dir: Path) -> None:
    """Create the data dir and seed ``.env`` (startup settings) from the bundled example."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(exist_ok=True)
    source = _resource_dir() / ".env.example"
    if not (data_dir / ".env").exists() and source.exists():
        shutil.copyfile(source, data_dir / ".env")


def main() -> None:
    multiprocessing.freeze_support()  # the Windows host child is spawned with multiprocessing
    parser = argparse.ArgumentParser(prog="vilagent-server", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--static-dir", type=Path, default=None, help="built operator UI to serve at /")
    parser.add_argument("--dev-origin", default=None, help="allow this UI origin through CORS (next dev)")
    parser.add_argument("--reload", action="store_true", help="reload on agent source changes (development)")
    args = parser.parse_args()

    if not getattr(sys, "frozen", False):
        # Keep imports working after the chdir below, also for reload/spawned children.
        agent_dir = str(Path(__file__).resolve().parents[1])
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        os.environ["PYTHONPATH"] = os.pathsep.join([agent_dir, os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)

    data_dir = (args.data_dir or _default_data_dir()).resolve()
    static_dir = args.static_dir.resolve() if args.static_dir else None  # before the chdir
    _bootstrap_data_dir(data_dir)
    os.chdir(data_dir)
    os.environ["VILAGENT_DATA_DIR"] = str(data_dir)
    os.environ["GATEWAY_HOST"] = args.host
    os.environ["GATEWAY_PORT"] = str(args.port)
    if static_dir:
        os.environ["VILAGENT_STATIC_DIR"] = str(static_dir)
    if args.dev_origin:
        os.environ["VILAGENT_DEV_ORIGIN"] = args.dev_origin

    import uvicorn

    package_dir = Path(__file__).resolve().parent
    uvicorn.run(
        "vilagent.server.app:app",
        host=args.host,
        port=args.port,
        http="h11",
        ws="none",
        reload=args.reload,
        reload_dirs=[str(package_dir)] if args.reload else None,
        **loop_kwargs(),
    )


if __name__ == "__main__":
    main()
