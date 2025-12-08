import pyautogui
import time
from typing import Union, Tuple

# Safety: Moving the mouse to any corner of the screen will trigger a FailSafeException, aborting the script.
pyautogui.FAILSAFE = True

# Helper Functions

def _transform_point(x: Union[int, float], y: Union[int, float]) -> Tuple[int, int]:
    """
    Transforms coordinates based on type:
    - If float (0.0 - 1.0): Converts relative position to absolute screen pixels.
    - If int: Treats as absolute screen pixels.
    """
    screen_w, screen_h = pyautogui.size()
    
    # Process X coordinate
    if isinstance(x, float) and 0.0 <= x <= 1.0:
        final_x = int(screen_w * x)
    else:
        final_x = int(x)
        
    # Process Y coordinate
    if isinstance(y, float) and 0.0 <= y <= 1.0:
        final_y = int(screen_h * y)
    else:
        final_y = int(y)
        
    return final_x, final_y

def _safe_execute(action_func, *args, **kwargs) -> str:
    """
    Wrapper to execute PyAutoGUI actions safely.
    Captures exceptions and returns a human/AI-readable string instead of crashing.
    """
    try:
        return action_func(*args, **kwargs)
    except pyautogui.FailSafeException:
        return "ABORTED: FailSafe triggered by user (Mouse moved to corner)."
    except Exception as e:
        return f"Action Error: {str(e)}"

# Main Tools

def move_and_click(x: Union[int, float], y: Union[int, float], button: str = "left", double: bool = False) -> str:
    """
    Moves the mouse to the specified coordinates and performs a click.
    
    Args:
        x: X coordinate (int for pixels, float 0.0-1.0 for relative width).
        y: Y coordinate (int for pixels, float 0.0-1.0 for relative height).
        button: 'left', 'right', or 'middle'.
        double: If True, performs a double click.
        
    Returns:
        String describing the result or error.
    """
    # 1. Transform coordinates
    abs_x, abs_y = _transform_point(x, y)
    screen_w, screen_h = pyautogui.size()
    
    # 2. Bounds Check
    if not (0 <= abs_x <= screen_w and 0 <= abs_y <= screen_h):
        return f"ERROR: Coordinates ({abs_x}, {abs_y}) are out of screen bounds ({screen_w}x{screen_h})."

    # 3. Define Action
    def _action():
        pyautogui.moveTo(abs_x, abs_y, duration=0.3)
        
        clicks = 2 if double else 1
        pyautogui.click(button=button, clicks=clicks)
        
        click_type = "Double" if double else "Single"
        coord_type = "(Relative)" if isinstance(x, float) else "(Absolute)"
        return f"Mouse moved to {coord_type} ({abs_x}, {abs_y}) and performed {click_type} {button} click."

    # 4. Execute Safely
    return _safe_execute(_action)

def scroll_page(amount: int) -> str:
    """
    Scrolls the page up or down.
    
    Args:
        amount: Integer value. Positive for UP, Negative for DOWN.
    """
    def _action():
        pyautogui.scroll(amount)
        direction = "UP" if amount > 0 else "DOWN"
        return f"Scrolled {direction} by {abs(amount)} units."

    return _safe_execute(_action)

def drag_mouse(start_x: Union[int, float], start_y: Union[int, float], end_x: Union[int, float], end_y: Union[int, float]) -> str:
    """
    Performs a drag-and-drop operation from start coordinates to end coordinates.
    Accepts both relative (float) and absolute (int) coordinates.
    """
    s_x, s_y = _transform_point(start_x, start_y)
    e_x, e_y = _transform_point(end_x, end_y)
    
    def _action():
        pyautogui.moveTo(s_x, s_y)
        # button='left' is default, duration ensures smooth drag
        pyautogui.dragTo(e_x, e_y, button='left', duration=0.5) 
        return f"Dragged mouse from ({s_x}, {s_y}) to ({e_x}, {e_y})."
        
    return _safe_execute(_action)

# Test Block
if __name__ == "__main__":
    print("TEST MODE: Testing relative coordinates...")
    print("Moving to center (0.5, 0.5) in 3 seconds...")
    time.sleep(3)
    
    # Test 1: Relative Coordinates
    result = move_and_click(0.5, 0.5, "left")
    print(f"Result: {result}")
    
    print("TEST FINISHED.")
