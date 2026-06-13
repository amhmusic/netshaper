# Fresh install python dependencies
sudo apt update
sudo apt install -y python3-pip python3-pil i2c-tools

sudo pip3 install adafruit-blinka --break-system-packages
sudo pip3 install adafruit-circuitpython-ssd1306 pillow --break-system-packages
sudo pip install uvicorn fastapi adafruit-ssd1306 pillow --break-system-packages

sudo mkdir -p /opt/tc-service
sudo cp tc_service.py /opt/tc-service/
sudo python3 -m venv /opt/tc-service/venv
sudo /opt/tc-service/venv/bin/pip install -r requirements.txt


sudo cp tc-service.service /etc/systemd/system/
sudo systemctl enable --now tc-service