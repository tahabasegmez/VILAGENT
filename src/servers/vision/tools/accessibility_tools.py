import sys
from typing import Dict, List, Any
from pywinauto import Desktop
from pywinauto.controls.uiawrapper import UIAWrapper


def get_focused_window_tree(max_depth: int = 5) -> str:
    """
    Returns the Accessibility Tree of the currently focused window.
    Format: [ID] Type "Name" (L, T, R, B)
    
    This helps the model understand the UI structure and find elements that
    might be hard to see visually.
    """
    if sys.platform != "win32":
        return "ERROR: Accessibility tools are only available on Windows."

    try:
        # 1. Connect to the visible desktop using UIA (UI Automation)
        desktop = Desktop(backend="uia")
        
        # 2. Get the foreground (active) window
        window = desktop.window(active_only=True)
        
        if not window.exists():
            return "ERROR: No active window found."

        # Header info
        output = [f"Active Window: '{window.window_text()}'"]
        output.append("Format: [Depth] ControlType 'Name' (Left, Top, Right, Bottom)")
        output.append("-" * 50)

        # 3. Recursive function to walk the tree
        def walk_tree(control, depth):
            if depth > max_depth:
                return

            try:
                # Get children of the current control
                children = control.children()
                
                for child in children:
                    # Filter: Only show visible elements to reduce noise
                    if not child.is_visible():
                        continue
                        
                    # Extract Data
                    rect = child.rectangle()
                    rect_str = f"({rect.left}, {rect.top}, {rect.right}, {rect.bottom})"
                    name = child.window_text()
                    control_type = child.element_info.control_type
                    
                    # Create indentation based on depth
                    indent = "  " * depth
                    
                    # Formatted Line: 
                    # [1] Button 'Submit' (100, 200, 150, 250)
                    line = f"{indent}- [{control_type}] '{name}' {rect_str}"
                    output.append(line)
                    
                    # Recursion
                    walk_tree(child, depth + 1)
                    
            except Exception:
                # Sometimes accessing a child fails (permission denied etc.), skip it
                pass

        # Start walking from the main window
        walk_tree(window.wrapper_object(), 0)

        return "\n".join(output)

    except Exception as e:
        return f"Tree Error: {str(e)}"
if __name__ == "__main__":
    print("TEST MODE: Inspecting focused window's accessibility tree...")
    print(get_focused_window_tree(max_depth=3))