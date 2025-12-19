"""
tools.py

LangGraph *local-only* tool implementations.

Rule applied:
- tools.py contains ONLY tools that are safer/more reliable to run locally and do not
  materially benefit from MCP server isolation.
- Anything that is heavy, model-backed, sensitive, or domain-isolated by design
  should be MCP (NOT implemented here):
    - OmniParser v2 parsing
    - any vision model inference
    - RAG/DB access
    - complex keyboard/mouse servers if you want strict isolation/auditing

Local tools included (minimal, high-utility):
- wait
- clipboard_get / clipboard_set (optional; local convenience)
- time_now_ms (diagnostics)
- no-op ping (diagnostics)

Local tools deliberately excluded (prefer MCP in your architecture):
- screen_capture (vision_server)  [centralized capture + hashing + storage]
- mouse actions (mouse_server)    [auditable, sandboxable; reduces local flakiness]
- keyboard actions (keyboard_server)
- UIA/pywinauto operations (uia_server) unless you explicitly keep UIA local
- omniparser_v2_parse (vision_server)
- any db/rag tools (db_server / rag_server)

If you later decide some excluded tool must be local, add it explicitly and document why.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .state import ToolResult, now_ms


# -----------------------------
# Helpers
# -----------------------------

def _ok(data: Any = None) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _err(msg: str) -> ToolResult:
    return ToolResult(ok=False, error=msg)


# -----------------------------
# Minimal local tools
# -----------------------------

def wait(args: Dict[str, Any]) -> ToolResult:
    """
    Local sleep. Safe and deterministic.
    Args:
      ms: int (default 250)
    """
    ms = int(args.get("ms", 250))
    time.sleep(max(ms, 0) / 1000.0)
    return _ok({"slept_ms": ms})


def time_now_ms(_: Dict[str, Any]) -> ToolResult:
    """
    Diagnostics. Useful for debugging latency and ordering.
    """
    return _ok({"ts_ms": now_ms()})


def ping(args: Dict[str, Any]) -> ToolResult:
    """
    Diagnostics. Returns echo + timestamp.
    Args:
      echo: any
    """
    return _ok({"echo": args.get("echo"), "ts_ms": now_ms()})


# -----------------------------
# Clipboard (local convenience)
# -----------------------------
# Clipboard is typically safe locally and often needed for UI workflows.
# If you want strict DLP / auditing, move clipboard to keyboard_server (MCP).

try:
    import pyperclip
except Exception:  # pragma: no cover
    pyperclip = None


def clipboard_get(_: Dict[str, Any]) -> ToolResult:
    if pyperclip is None:
        return _err("MISSING_DEPENDENCY: pyperclip")
    try:
        return _ok({"text": pyperclip.paste()})
    except Exception as e:
        return _err(f"CLIPBOARD_GET_ERROR: {type(e).__name__}: {e}")


def clipboard_set(args: Dict[str, Any]) -> ToolResult:
    if pyperclip is None:
        return _err("MISSING_DEPENDENCY: pyperclip")
    try:
        text = str(args.get("text", ""))
        pyperclip.copy(text)
        return _ok({"len": len(text)})
    except Exception as e:
        return _err(f"CLIPBOARD_SET_ERROR: {type(e).__name__}: {e}")


# -----------------------------
# Export
# -----------------------------

def get_local_tools() -> Dict[str, Any]:
    """
    Return mapping suitable for ToolRegistry(local_tools=...).
    """
    return {
        "wait": wait,
        "time_now_ms": time_now_ms,
        "ping": ping,
        # clipboard (optional)
        "clipboard_get": clipboard_get,
        "clipboard_set": clipboard_set,
    }


# -----------------------------
# MCP-first tool list (for your reference, NOT implemented here)
# -----------------------------
"""
Prefer MCP servers for the following:

vision_server:
- screen_capture
- region_capture
- omniparser_v2_parse
- screenshot_diff / change_detection
- element_grounding (bbox -> selector candidates)

mouse_server:
- click / double_click / right_click
- move / drag / scroll
- get_cursor_pos

keyboard_server:
- type_text / hotkey
- key_down / key_up
- clipboard_get / clipboard_set (if you enforce DLP/audit here instead of local)

uia_server (either local OR MCP; choose one strategy and stay consistent):
- focus_window
- list_windows
- uia_tree
- uia_find(selector) -> stable path/runtime_id
- uia_click / uia_set_text / uia_invoke / uia_toggle / uia_select
- wait_for_element / wait_for_window

db_server / rag_server:
- rag_query
- memory_read / memory_write
- cache_put / cache_get
"""


# -----------------------------------------------------------------------------
# DERS NOTU (local_tools.py) — Lokal (MCP dışı) tool implementasyonları
# -----------------------------------------------------------------------------
# Bu dosya, “lokalde çalıştırılması güvenli ve deterministik” olan küçük araçları
# (tool) içerir. Mimari prensip:
#
# - Ağır / model tabanlı / hassas veya izolasyon gerektiren şeyler MCP server’da.
# - Basit, düşük riskli, deterministik yardımcılar local.
#
# Bu ayrımın faydası:
# - Güvenlik: Kritik tool’lar (mouse/keyboard/vision) izole edilir.
# - Audit: Tool trafiği merkezi log’lanabilir.
# - Stabilite: GUI otomasyonu gibi flakey işler ayrıştırılır.
#
# Dosyadaki ana parçalar:
#
# 1) ToolResult yardımcıları
#    - _ok(data): ToolResult(ok=True, data=...)
#    - _err(msg): ToolResult(ok=False, error=...)
#
# 2) Minimal local tool’lar
#    - wait(args): ms kadar uyur.
#      args["ms"] yoksa 250ms.
#      time.sleep saniye cinsinden aldığı için ms/1000.0 yapılır.
#    - time_now_ms(_): state.now_ms() ile timestamp döner.
#    - ping(args): echo + timestamp döner.
#
# 3) Clipboard araçları (opsiyonel)
#    - pyperclip import’u try/except ile yapılır:
#      bağımlılık yoksa pyperclip = None.
#    - clipboard_get / clipboard_set:
#      - bağımlılık yoksa MISSING_DEPENDENCY döner.
#      - aksi halde kopyala/yapıştır işlemi.
#
# 4) get_local_tools()
#    - ToolRegistry’ye verilecek mapping’i döndürür.
#    - workflow.py içinde ToolRegistry(local_tools=...) şeklinde kullanılmak üzere.
#
# 5) Dosyanın sonundaki “MCP-first tool list”
#    - Bu bir referans notudur; burada implement edilmez.
#    - Ama mimari olarak hangi tool’ların hangi server’da olması gerektiğini
#      “takım içi sözleşme” gibi açıklar.
#
# Not:
# - Bu modül adı local_tools.py, ama üst docstring içinde “tools.py” ifadesi geçiyor.
#   Bu sadece isimlendirme notu; çalışma mantığını etkilemez.

