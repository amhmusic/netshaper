import subprocess
import re
import time
import board
import busio
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306

INTERFACE = "wlan0"  # Change to your interface
REFRESH_INTERVAL = 2  # Seconds between updates

def get_ip(interface):
    try:
        out = subprocess.check_output(
            ["ip", "addr", "show", interface],
            stderr=subprocess.DEVNULL
        ).decode()
        match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
        return match.group(1) if match else "No IP"
    except subprocess.CalledProcessError:
        return "Error"

def get_tc_settings(interface):
    result = {
        "download": "N/A",
        "latency": "N/A",
        "loss": "N/A",
    }

    try:
        out = subprocess.check_output(
            ["tc", "qdisc", "show", "dev", interface],
            stderr=subprocess.DEVNULL
        ).decode()

        rate_match = re.search(r'rate (\S+)', out)
        if rate_match:
            result["download"] = rate_match.group(1)

        delay_match = re.search(r'delay (\S+)', out)
        if delay_match:
            result["latency"] = delay_match.group(1)

        loss_match = re.search(r'loss (\S+)', out)
        if loss_match:
            result["loss"] = loss_match.group(1)

    except subprocess.CalledProcessError:
        pass

    return result

# --- Display setup (done once) ---
i2c = busio.I2C(board.SCL, board.SDA)
display = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c)
font = ImageFont.load_default()
line_height = 10

try:
    while True:
        tc = get_tc_settings(INTERFACE)
        ip = get_ip(INTERFACE)

        image = Image.new("1", (display.width, display.height))
        draw = ImageDraw.Draw(image)

        draw.text((0, 0),                f"{ip}",                   font=font, fill=255)
        draw.text((0, line_height * 2),  f"BW:   {tc['download']}", font=font, fill=255)
        draw.text((0, line_height * 3),  f"Lat:  {tc['latency']}",  font=font, fill=255)
        draw.text((0, line_height * 4),  f"Loss: {tc['loss']}",     font=font, fill=255)

        display.image(image)
        display.show()

        time.sleep(REFRESH_INTERVAL)

except KeyboardInterrupt:
    # Clean up display on Ctrl+C
    display.fill(0)
    display.show()