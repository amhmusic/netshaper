# netshaper

A single Rust service that:

- Drives an SSD1306/SSD1305/SSD1315 OLED HAT (I2C) showing IP + current shaper state
- Provides a JSON HTTP API on port 8080 to configure `tc`/`netem` rules
- Cleans up qdiscs on reset

## Display layout (128×64)

```
eth0: 192.168.1.42
BW:  1000 kbps
Lat: 100ms  Jit: 10ms
Loss:5%  Cor:0%
[  ACTIVE  ]
```

## API

|Method|Path   |Body / Notes                      |
|------|-------|----------------------------------|
|GET   |/config|Returns current ShaperConfig JSON |
|POST  |/config|Accepts ShaperConfig JSON, applies|
|POST  |/reset |Clears all qdiscs, marks inactive |
|GET   |/ip    |Returns `{"ip":"..."}` for eth0   |

### ShaperConfig JSON shape

```json
{
  "interface":      "eth1",
  "bandwidth_kbps": 1000,
  "latency_ms":     100,
  "jitter_ms":      10,
  "loss_pct":       5.0,
  "corrupt_pct":    0.0,
  "active":         true
}
```

Set `bandwidth_kbps` to `0` for unlimited. Set `active` to `false` to clear rules.

### Example — simulate a bad 3G connection

```bash
curl -X POST http://<pi-ip>:8080/config \
  -H 'Content-Type: application/json' \
  -d '{
    "interface":      "eth1",
    "bandwidth_kbps": 400,
    "latency_ms":     150,
    "jitter_ms":      30,
    "loss_pct":       2.0,
    "corrupt_pct":    0.0,
    "active":         true
  }'
```

## Prerequisites on the Pi

```bash
sudo apt install -y iproute2        # provides `tc`
sudo raspi-config                   # Interface Options → I2C → Enable
```

## Build (on the Pi or cross-compile)

```bash
# On the Pi directly
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
cd netshaper
cargo build --release
sudo cp target/release/netshaper /usr/local/bin/
```

### Cross-compile from macOS/Linux (faster)

```bash
# Install cross
cargo install cross

# Build for Pi 5 (aarch64) or Pi 4/3 (armv7)
cross build --release --target aarch64-unknown-linux-gnu
# or
cross build --release --target armv7-unknown-linux-gnueabihf

scp target/aarch64-unknown-linux-gnu/release/netshaper pi@<pi-ip>:/usr/local/bin/
```

## Install as a systemd service

```bash
sudo cp netshaper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now netshaper
sudo journalctl -fu netshaper   # watch logs
```

## OLED wiring (I2C)

|OLED pin|Pi GPIO (physical)|
|--------|------------------|
|VCC     |Pin 1  (3.3V)     |
|GND     |Pin 6  (GND)      |
|SDA     |Pin 3  (GPIO 2)   |
|SCL     |Pin 5  (GPIO 3)   |

If your HAT plugs directly onto the 40-pin header, it’s already wired correctly.

## Troubleshooting

```bash
# Check I2C device is visible (should show address 0x3c or 0x3d)
sudo i2cdetect -y 1

# Check tc is working
sudo tc qdisc show dev eth1

# Tail service logs
sudo journalctl -fu netshaper
```