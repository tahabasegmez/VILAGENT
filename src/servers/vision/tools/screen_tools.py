import pyautogui

def get_resolution() -> str:
    width, height = pyautogui.size()
    return f"{width}x{height}"