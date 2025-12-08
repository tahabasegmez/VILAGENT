import sys
import time
# platform check only for import safety on non-windows dev environments
from pywinauto import Desktop
from pywinauto.application import Application

def _check_windows():
    if sys.platform != "win32":
        return "ERROR: Window tools are only supported on Windows OS."
    return None

def list_open_windows() -> str:
    """
    Lists all currently visible application windows.
    Useful for finding the exact title needed to focus a window.
    """
    error = _check_windows()
    if error: return error

    try:
        # Using UIA backend is usually better for modern Windows apps
        desktop = Desktop(backend="uia")
        windows = desktop.windows()
        
        window_list = []
        for w in windows:
            if w.is_visible() and w.window_text():
                window_list.append(f"- {w.window_text()}")
        
        if not window_list:
            return "No visible windows found."
            
        return "Visible Windows:\n" + "\n".join(window_list)
    except Exception as e:
        return f"Error listing windows: {str(e)}"

def focus_window(title_query: str) -> str:
    """
    Brings a window containing the specified text to the foreground (Focus).
    
    Args:
        title_query: Part of the window title (e.g., 'Notepad' for 'Untitled - Notepad').
                     Case-insensitive.
    """
    error = _check_windows()
    if error: return error

    try:
        desktop = Desktop(backend="uia")
        # Use regex matching for partial titles
        # .*query.* means "anything before AND anything after" the query
        window = desktop.window(title_re=f".*{title_query}.*")
        
        if window.exists():
            window.set_focus()
            # Sometimes a second click is needed to ensure input focus
            return f"SUCCESS: Window matching '{title_query}' is now in focus."
        else:
            return f"ERROR: No window found containing '{title_query}'."
            
    except Exception as e:
        # If multiple windows match, pywinauto might raise an error
        return f"Focus Error (Maybe multiple windows match?): {str(e)}"

if __name__ == "__main__":
    print("TEST MODE: Testing relative coordinates...")
    print("Moving to center (0.5, 0.5) in 3 seconds...")
    time.sleep(3)
    
    # Test 1: Relative Coordinates
    result = list_open_windows()
    print(f"Result: {result}")
    
    print("TEST FINISHED.")