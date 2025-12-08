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
    from tools import screen_tools, window_tools, accessibility_tools, omni_tools
except ImportError as e:
    print(f"CRITICAL IMPORT ERROR: {e}", file=sys.stderr)
    raise e

mcp = FastMCP("Vilagent Vision Server")

# Window Tools
@mcp.tool()
def list_running_apps() -> str:
    """
    Lists the titles of all open and visible windows. 
    Use this before 'focus_app' to find the correct name.
    """
    return window_tools.list_open_windows()

@mcp.tool()
def focus_app(app_name: str) -> str:
    """
    Brings an application to the front.
    Args:
        app_name: Partial name of the window (e.g., 'Code' for VS Code, 'Chrome' for Browser).
    """
    return window_tools.focus_window(app_name)

# Info Tools
@mcp.tool()
def get_screen_resolution() -> str:
    """Returns screen width and height (e.g. '1920x1080')."""
    return screen_tools.get_resolution() 

@mcp.tool()
def inspect_ui_tree() -> str:
    """
    Dumps the accessibility tree of the focused window.
    
    Returns a hierarchical text map of UI elements including:
    - Control Type (Button, Edit, List, etc.)
    - Name/Text (What is written on it)
    - Coordinates (Left, Top, Right, Bottom) - ABSOLUTE PIXELS
    
    USE THIS WHEN:
    1. You need to find a specific button's coordinates to click.
    2. You cannot read the text on the screen clearly via Vision.
    3. You need to understand the structure of a complex form.
    """
    return accessibility_tools.get_focused_window_tree()

@mcp.tool()
def analyze_screen_using_omni() -> str:
    """
    Captures the current screen and sends it to the OmniParser model (Remote).
    
    Returns:
        A structured text list of UI elements (Buttons, Icons, Text) present on the screen,
        along with their coordinates or descriptions.
        
    Usage:
        Call this tool when you don't know the coordinates of a button 
        or need to understand the screen layout visually.
    """
    return omni_tools.capture_and_send()

if __name__ == "__main__":
    mcp.run()