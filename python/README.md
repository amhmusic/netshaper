# Fresh install python dependencies
sudo apt update
sudo apt install -y python3-pip python3-pil i2c-tools

sudo pip3 install adafruit-blinka --break-system-packages
sudo pip3 install adafruit-circuitpython-ssd1306 pillow --break-system-packages
