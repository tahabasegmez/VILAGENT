import os
import requests
import mss
from PIL import Image
from io import BytesIO
from typing import Optional


# Example: "https://a1b2-34-125.ngrok-free.app"
COLAB_API_URL = os.getenv("OMNI_COLAB_URL", "BURAYA_COLAB_URL_YAPISTIR")

def capture_and_send() -> str:
    """
    Captures the screen, sends it to the Google Colab OmniParser API,
    and returns the analyzed UI elements text.
    """
    if "ngrok" not in COLAB_API_URL and "localtunnel" not in COLAB_API_URL:
        return "ERROR: Colab URL is not configured in omni_colab.py or .env"

    try:
        # Screen Capture
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            

            # img.thumbnail((1280, 1280)) 

            # Convert image to bytes (to send as a file)
            img_byte_arr = BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=80)
            img_byte_arr.seek(0)

        # Send POST request to Colab
        print(f"📡 Sending screenshot to Colab: {COLAB_API_URL}...")
        
        files = {'image': ('screen.jpg', img_byte_arr, 'image/jpeg')}
        response = requests.post(f"{COLAB_API_URL}/analyze", files=files, timeout=30)

        if response.status_code == 200:
            data = response.json()
            return f"--- COLAB OMNIPARSER RESULT ---\n{data.get('text', 'No text returned')}"
        else:
            return f"Colab Error ({response.status_code}): {response.text}"

    except requests.exceptions.ConnectionError:
        return "ERROR: Could not connect to Colab. Check if the URL is active and ngrok is running."
    except Exception as e:
        return f"Local Error: {str(e)}"

if __name__ == "__main__":
    print(capture_and_send())