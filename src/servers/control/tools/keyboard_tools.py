import pyautogui
import pyperclip
import platform
import time

# Safety check
pyautogui.FAILSAFE = True

def type_text(text: str) -> str:
    """
    Types text using the Clipboard (Copy-Paste) method.
    This is much more reliable for non-ASCII characters (like Turkish 'ğ, ş, İ') 
    and faster than keystroke simulation.
    """
    try:
        # 1. Copy text to system clipboard
        pyperclip.copy(text)
        
        # 2. Determine the paste command based on OS
        # macOS uses 'command', Windows/Linux use 'ctrl'
        modifier = "command" if platform.system() == "Darwin" else "ctrl"
        
        # 3. Simulate Paste (Ctrl+V)
        pyautogui.hotkey(modifier, "v")
        
        # Small buffer to ensure paste operation completes before next action
        time.sleep(0.1) 
        
        return f"Typed successfully via clipboard: '{text}'"
    except Exception as e:
        return f"Typing Error: {str(e)}"

def press_hotkey(key_combo: str) -> str:
    """
    Presses a hotkey combination (e.g., 'ctrl+c', 'alt+tab', 'enter').
    """
    try:
        # Split the combo string into a list (e.g. "ctrl+c" -> ["ctrl", "c"])
        keys = key_combo.split("+")
        
        # Clean up keys (trim spaces just in case)
        keys = [k.strip() for k in keys]
        
        # Unpack the list into arguments for pyautogui
        pyautogui.hotkey(*keys) 
        
        return f"Hotkey pressed: {key_combo}"
    except Exception as e:
        return f"Hotkey Error: {str(e)}"