#!/usr/bin/env python3
"""
tc-service: REST API for traffic control (tc) + SSD1306 OLED display.

The FastAPI server and the display loop run concurrently in the same process:
  - A background thread drives the OLED, reading shared state.
  - The API thread handles HTTP requests and writes shared state.
  - A threading.Lock keeps them consistent.

REST API
--------
GET  /health             Liveness check
GET  /tc                 Current settings for both interfaces
GET  /tc/{iface}         Current settings for one interface
PUT  /tc/{iface}         Apply settings to one interface
DELETE /tc/{iface}       Clear settings on one interface
PUT  /tc                 Apply settings to both interfaces
DELETE /tc               Clear both interfaces

Swagger UI available at http://<pi>:8080/docs
"""

import subprocess
import re
import time
import threading
import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

import board
import busio
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

IFACE_IN          = "eth0"
IFACE_OUT         = "eth1"
DISPLAY_IFACE     = "wlan0"   # Interface shown on the OLED for the IP address
BIND_HOST         = "0.0.0.0"
BIND_PORT         = 8080
REFRESH_INTERVAL  = 2         # seconds between OLED refreshes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Shared state
# ──────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()

# Mirrors what has been successfully applied to the kernel.
_state: dict[str, dict] = {
    IFACE_IN:  {"active": False, "settings": {}},
    IFACE_OUT: {"active": False, "settings": {}},
}

def _get_state(iface: str) -> dict:
    with _lock:
        return dict(_state[iface])

def _set_state(iface: str, settings: dict, active: bool) -> None:
    with _lock:
        _state[iface] = {"active": active, "settings": settings}

# ──────────────────────────────────────────────────────────────────────────────
# tc helpers
# ──────────────────────────────────────────────────────────────────────────────

def _run_tc(*args: str) -> None:
    """Run a tc command; raise RuntimeError on failure."""
    cmd = ["tc", *args]
    log.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"tc exited {result.returncode}")


def _delete_root_qdisc(iface: str) -> None:
    """Remove the root qdisc; silently ignore 'nothing to remove' errors."""
    result = subprocess.run(
        ["tc", "qdisc", "del", "dev", iface, "root"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip()
        # Not an error if there was nothing to delete
        if "No such file" not in msg and "Cannot find" not in msg:
            raise RuntimeError(msg)


def tc_apply(iface: str, settings: dict) -> None:
    """
    Apply traffic-shaping settings to `iface`.

    Qdisc stack:
      With bandwidth:    root → tbf (rate limit) → netem (delay / loss)
      Without bandwidth: root → netem (delay / loss)
    """
    _delete_root_qdisc(iface)

    if not settings:
        return  # cleared

    bandwidth_kbps  = settings.get("bandwidth_kbps")
    latency_ms      = settings.get("latency_ms")
    jitter_ms       = settings.get("jitter_ms")
    loss_percent    = settings.get("loss_percent")
    duplicate_pct   = settings.get("duplicate_percent")
    corrupt_pct     = settings.get("corrupt_percent")
    reorder_pct     = settings.get("reorder_percent")

    def netem_args() -> list[str]:
        args = []
        if latency_ms is not None:
            args += ["delay", f"{latency_ms}ms"]
            if jitter_ms is not None:
                args += [f"{jitter_ms}ms", "distribution", "normal"]
        if loss_percent is not None:
            args += ["loss", f"{loss_percent:.4f}%"]
        if duplicate_pct is not None:
            args += ["duplicate", f"{duplicate_pct:.4f}%"]
        if corrupt_pct is not None:
            args += ["corrupt", f"{corrupt_pct:.4f}%"]
        if reorder_pct is not None:
            args += ["reorder", f"{reorder_pct:.4f}%", "5"]
        return args

    if bandwidth_kbps:
        burst = max(bandwidth_kbps * 1000 // 800, 4096)   # ~10 ms burst
        limit = max(bandwidth_kbps * 1000 // 80,  32768)  # ~100 ms queue

        _run_tc("qdisc", "add", "dev", iface,
                "root", "handle", "1:",
                "tbf",
                "rate", f"{bandwidth_kbps}kbit",
                "burst", str(burst),
                "limit", str(limit))

        n_args = netem_args()
        if n_args:
            _run_tc("qdisc", "add", "dev", iface,
                    "parent", "1:1", "handle", "10:",
                    "netem", *n_args)
    else:
        n_args = netem_args()
        if n_args:
            _run_tc("qdisc", "add", "dev", iface,
                    "root", "handle", "1:",
                    "netem", *n_args)


def tc_read(iface: str) -> dict:
    """
    Parse `tc qdisc show dev <iface>` and return a best-effort dict of
    the active settings. Used by the OLED display thread.
    """
    result = {"download": "N/A", "latency": "N/A", "loss": "N/A"}
    try:
        out = subprocess.check_output(
            ["tc", "qdisc", "show", "dev", iface],
            stderr=subprocess.DEVNULL,
        ).decode()
        if m := re.search(r'rate (\S+)', out):
            result["download"] = m.group(1)
        if m := re.search(r'delay (\S+)', out):
            result["latency"] = m.group(1)
        if m := re.search(r'loss (\S+)', out):
            result["loss"] = m.group(1)
    except subprocess.CalledProcessError:
        pass
    return result


def get_ip(interface: str) -> str:
    try:
        out = subprocess.check_output(
            ["ip", "addr", "show", interface],
            stderr=subprocess.DEVNULL,
        ).decode()
        if m := re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out):
            return m.group(1)
        return "No IP"
    except subprocess.CalledProcessError:
        return "Error"

# ──────────────────────────────────────────────────────────────────────────────
# OLED display loop  (your original code, adapted to run in a thread)
# ──────────────────────────────────────────────────────────────────────────────

def display_loop(stop_event: threading.Event) -> None:
    i2c     = busio.I2C(board.SCL, board.SDA)
    display = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c)
    font    = ImageFont.load_default()
    line_h  = 10

    try:
        while not stop_event.is_set():
            tc      = tc_read(DISPLAY_IFACE)
            ip      = get_ip(DISPLAY_IFACE)

            image = Image.new("1", (display.width, display.height))
            draw  = ImageDraw.Draw(image)

            draw.text((0, 0),           ip,                     font=font, fill=255)
            draw.text((0, line_h * 2),  f"BW:   {tc['download']}", font=font, fill=255)
            draw.text((0, line_h * 3),  f"Lat:  {tc['latency']}",  font=font, fill=255)
            draw.text((0, line_h * 4),  f"Loss: {tc['loss']}",     font=font, fill=255)

            display.image(image)
            display.show()

            stop_event.wait(REFRESH_INTERVAL)
    finally:
        display.fill(0)
        display.show()
        log.info("Display cleared")

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────

_stop_display = threading.Event()

@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=display_loop, args=(_stop_display,), daemon=True)
    t.start()
    log.info("Display thread started")
    yield
    _stop_display.set()
    t.join(timeout=5)
    log.info("Display thread stopped")

app = FastAPI(
    title="tc-service",
    description="REST API for Linux traffic control (tc) on Raspberry Pi",
    version="1.0.0",
    lifespan=lifespan,
)

MANAGED_IFACES = {IFACE_IN, IFACE_OUT}

def _require_iface(iface: str) -> None:
    if iface not in MANAGED_IFACES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown interface '{iface}'. Managed: {', '.join(sorted(MANAGED_IFACES))}",
        )

# ── Pydantic models ───────────────────────────────────────────────────────────

class TcSettings(BaseModel):
    latency_ms:        Optional[float] = Field(None, ge=0,   le=60_000, description="Added latency in ms")
    jitter_ms:         Optional[float] = Field(None, ge=0,   le=10_000, description="Latency jitter in ms (requires latency_ms)")
    loss_percent:      Optional[float] = Field(None, ge=0.0, le=100.0,  description="Packet loss %")
    bandwidth_kbps:    Optional[int]   = Field(None, gt=0,              description="Bandwidth cap in kbit/s")
    duplicate_percent: Optional[float] = Field(None, ge=0.0, le=100.0,  description="Packet duplication %")
    corrupt_percent:   Optional[float] = Field(None, ge=0.0, le=100.0,  description="Packet corruption %")
    reorder_percent:   Optional[float] = Field(None, ge=0.0, le=100.0,  description="Packet reordering % (requires latency_ms)")

    @model_validator(mode="after")
    def check_dependencies(self):
        if self.jitter_ms is not None and self.latency_ms is None:
            raise ValueError("jitter_ms requires latency_ms to also be set")
        if self.reorder_percent is not None and self.latency_ms is None:
            raise ValueError("reorder_percent requires latency_ms to also be set")
        return self

    def is_empty(self) -> bool:
        return all(v is None for v in self.model_dump().values())

    def to_tc_dict(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class BothSettings(BaseModel):
    """Body for PUT /tc — optional per-interface overrides, or shared top-level fields."""
    eth0: Optional[TcSettings] = None
    eth1: Optional[TcSettings] = None
    # shared fallback fields
    latency_ms:        Optional[float] = Field(None, ge=0,   le=60_000)
    jitter_ms:         Optional[float] = Field(None, ge=0,   le=10_000)
    loss_percent:      Optional[float] = Field(None, ge=0.0, le=100.0)
    bandwidth_kbps:    Optional[int]   = Field(None, gt=0)
    duplicate_percent: Optional[float] = Field(None, ge=0.0, le=100.0)
    corrupt_percent:   Optional[float] = Field(None, ge=0.0, le=100.0)
    reorder_percent:   Optional[float] = Field(None, ge=0.0, le=100.0)

    def settings_for(self, iface: str) -> TcSettings:
        per_iface = getattr(self, iface.replace("-", "_"), None)
        if per_iface:
            return per_iface
        # Fall back to shared top-level fields
        return TcSettings(
            latency_ms=self.latency_ms,
            jitter_ms=self.jitter_ms,
            loss_percent=self.loss_percent,
            bandwidth_kbps=self.bandwidth_kbps,
            duplicate_percent=self.duplicate_percent,
            corrupt_percent=self.corrupt_percent,
            reorder_percent=self.reorder_percent,
        )

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/tc", tags=["tc"])
def get_all():
    """Return current tc settings for both managed interfaces."""
    with _lock:
        return {"interfaces": dict(_state)}


@app.get("/tc/{iface}", tags=["tc"])
def get_iface(iface: str):
    """Return current tc settings for one interface."""
    _require_iface(iface)
    return {"interface": iface, **_get_state(iface)}


@app.put("/tc/{iface}", tags=["tc"])
def apply_iface(iface: str, settings: TcSettings):
    """Apply traffic-shaping settings to one interface."""
    _require_iface(iface)
    tc_dict = settings.to_tc_dict()
    try:
        tc_apply(iface, tc_dict)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"tc error: {e}")
    _set_state(iface, tc_dict, active=not settings.is_empty())
    return {
        "interface": iface,
        "active": not settings.is_empty(),
        "applied": tc_dict,
    }


@app.delete("/tc/{iface}", tags=["tc"])
def clear_iface(iface: str):
    """Remove all qdiscs from one interface (restore pass-through)."""
    _require_iface(iface)
    try:
        tc_apply(iface, {})
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"tc error: {e}")
    _set_state(iface, {}, active=False)
    return {"interface": iface, "active": False, "message": f"Cleared {iface}"}


@app.put("/tc", tags=["tc"])
def apply_both(payload: BothSettings):
    """Apply settings to both interfaces. Supports per-interface overrides."""
    errors = []
    for iface in (IFACE_IN, IFACE_OUT):
        s = payload.settings_for(iface)
        tc_dict = s.to_tc_dict()
        try:
            tc_apply(iface, tc_dict)
            _set_state(iface, tc_dict, active=not s.is_empty())
        except RuntimeError as e:
            errors.append(f"{iface}: {e}")
    if errors:
        raise HTTPException(status_code=500, detail="; ".join(errors))
    with _lock:
        return {"interfaces": dict(_state)}


@app.delete("/tc", tags=["tc"])
def clear_both():
    """Clear traffic shaping on both interfaces."""
    errors = []
    for iface in (IFACE_IN, IFACE_OUT):
        try:
            tc_apply(iface, {})
            _set_state(iface, {}, active=False)
        except RuntimeError as e:
            errors.append(f"{iface}: {e}")
    if errors:
        raise HTTPException(status_code=500, detail="; ".join(errors))
    return {"message": "Cleared all interfaces", "interfaces": [IFACE_IN, IFACE_OUT]}


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host=BIND_HOST, port=BIND_PORT, log_level="info")