from mcp.server.fastmcp import FastMCP
import sys
import os
from typing import Union

# Path Configuration
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# Import Tools
try:
    from tools import mouse_tools, keyboard_tools
except ImportError as e:
    print(f"CRITICAL IMPORT ERROR: {e}", file=sys.stderr)
    raise e

mcp = FastMCP("Vilagent Control Server")

# Mouse Tools
@mcp.tool()
def move_and_click(x: Union[int, float], y: Union[int, float], button: str = "left", double: bool = False) -> str:
    """
    Moves the mouse to the specified coordinates and performs a click.
    Args:
        x: X coord (int for pixels, float for relative).
        y: Y coord (int for pixels, float for relative).
        button: 'left', 'right', 'middle'.
        double: True for double-click.
    """
    return mouse_tools.move_and_click(x, y, button, double)

@mcp.tool()
def drag_mouse(start_x: Union[int, float], start_y: Union[int, float], end_x: Union[int, float], end_y: Union[int, float]) -> str:
    """Drag-and-drop operation."""
    return mouse_tools.drag_mouse(start_x, start_y, end_x, end_y)

@mcp.tool()
def scroll_page(amount: int) -> str:
    """Scrolls vertically. Positive=UP, Negative=DOWN."""
    return mouse_tools.scroll_page(amount)

# Keyboard Tools
@mcp.tool()
def type_text(text: str) -> str:
    """Types text. IMPORTANT: Use 'focus_window' tool first to ensure the correct app receives the text."""
    return keyboard_tools.type_text(text)

@mcp.tool()
def press_hotkey(keys: str) -> str:
    """Presses hotkeys (e.g. 'ctrl+c', 'win', 'alt+tab')."""
    return keyboard_tools.press_hotkey(keys)


if __name__ == "__main__":
    mcp.run()