# PyInstaller spec for the VILAGENT gateway (run via `pnpm build:server` in desktop/).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

agent = Path(SPECPATH)
repo = agent.parent

datas = [(str(repo / ".env.example"), ".")]
binaries = []
# uvicorn imports the app by string and the model factory imports providers by name.
# win32crypt protects the saved API keys (connections.py) and is imported lazily.
hiddenimports = collect_submodules("vilagent") + collect_submodules("uvicorn") + ["win32crypt"]
for package in ("playwright", "pywinauto", "comtypes", "pyautogui", "langchain_openai", "langchain_google_genai", "langchain_huggingface", "langchain_ollama", "langchain_anthropic", "langgraph", "aiosqlite"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

# The run graph's checkpoints use SQLite. Conda-style Pythons keep its DLL in Library\bin,
# where PyInstaller does not look, so _sqlite3 would fail to load in the frozen app.
sqlite_dll = Path(sys.base_prefix) / "Library" / "bin" / "sqlite3.dll"
if sqlite_dll.exists():
    binaries.append((str(sqlite_dll), "."))

# Optional imports of the dependency tree that VILAGENT never uses (~600 MB with MKL).
excludes = ["numpy", "pandas", "pyarrow", "scipy", "matplotlib", "cv2", "IPython", "tkinter", "torch"]

a = Analysis(
    [str(agent / "vilagent" / "__main__.py")],
    pathex=[str(agent)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=excludes,
)
pyz = PYZ(a.pure)
# console=True keeps stdout/stderr valid for the launcher's log pipes (it hides the window).
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="vilagent-server", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="vilagent-server")
