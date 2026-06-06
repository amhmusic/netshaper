use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tiny_http::{Response, Server, Header};

// ── Display driver (SSD1306 / SSD1305 / SSD1315 via I2C) ─────────────────────
// Uses the `ssd1306` + `linux-embedded-hal` crates.
use embedded_graphics::{
    mono_font::{ascii::FONT_6X10, MonoTextStyleBuilder},
    pixelcolor::BinaryColor,
    prelude::*,
    text::{Baseline, Text},
};
use linux_embedded_hal::I2cdev;
use ssd1306::{prelude::*, I2CDisplayInterface, Ssd1306};

// ── Shared state ──────────────────────────────────────────────────────────────

#[derive(Clone, Serialize, Deserialize, Debug)]
struct ShaperConfig {
    /// Network interface that sits between the two sides (e.g. "eth1")
    interface: String,
    /// Bandwidth cap in kbit/s (0 = unlimited)
    bandwidth_kbps: u32,
    /// One-way added latency in ms
    latency_ms: u32,
    /// Jitter in ms (applied on top of latency)
    jitter_ms: u32,
    /// Packet loss percentage (0–100)
    loss_pct: f32,
    /// Packet corruption percentage (0–100)
    corrupt_pct: f32,
    /// Whether shaping is currently active
    active: bool,
}

impl Default for ShaperConfig {
    fn default() -> Self {
        Self {
            interface: "eth1".into(),
            bandwidth_kbps: 0,
            latency_ms: 0,
            jitter_ms: 0,
            loss_pct: 0.0,
            corrupt_pct: 0.0,
            active: false,
        }
    }
}

type SharedConfig = Arc<Mutex<ShaperConfig>>;

// ── tc/netem helpers ──────────────────────────────────────────────────────────

fn tc(args: &[&str]) -> Result<(), String> {
    let status = Command::new("tc")
        .args(args)
        .status()
        .map_err(|e| e.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("tc exited with {:?}", status.code()))
    }
}

/// Remove any existing qdiscs on the interface (best-effort).
fn clear_qdiscs(iface: &str) {
    let _ = tc(&["qdisc", "del", "dev", iface, "root"]);
    let _ = tc(&["qdisc", "del", "dev", iface, "ingress"]);
}

/// Apply (or re-apply) the current config to the kernel.
fn apply_config(cfg: &ShaperConfig) -> Result<(), String> {
    clear_qdiscs(&cfg.interface);

    if !cfg.active {
        return Ok(()); // cleared — done
    }

    // Build the netem options string
    let mut netem_opts: Vec<String> = vec![];

    if cfg.latency_ms > 0 {
        if cfg.jitter_ms > 0 {
            netem_opts.push(format!("delay {}ms {}ms", cfg.latency_ms, cfg.jitter_ms));
        } else {
            netem_opts.push(format!("delay {}ms", cfg.latency_ms));
        }
    }
    if cfg.loss_pct > 0.0 {
        netem_opts.push(format!("loss {:.1}%", cfg.loss_pct));
    }
    if cfg.corrupt_pct > 0.0 {
        netem_opts.push(format!("corrupt {:.1}%", cfg.corrupt_pct));
    }

    if cfg.bandwidth_kbps > 0 {
        // Token bucket filter (TBF) as root, netem as child
        tc(&[
            "qdisc", "add", "dev", &cfg.interface,
            "root", "handle", "1:",
            "tbf",
            "rate", &format!("{}kbit", cfg.bandwidth_kbps),
            "burst", "32kbit",
            "latency", "400ms",
        ])?;

        if !netem_opts.is_empty() {
            let mut args = vec![
                "qdisc", "add", "dev", &cfg.interface,
                "parent", "1:1", "handle", "10:",
                "netem",
            ];
            let opts_str = netem_opts.join(" ");
            // Split back into individual words for Command
            let words: Vec<&str> = opts_str.split_whitespace().collect();
            args.extend(words.iter().copied());
            tc(&args)?;
        }
    } else if !netem_opts.is_empty() {
        // netem only (no bandwidth cap)
        let mut args = vec![
            "qdisc", "add", "dev", &cfg.interface,
            "root", "handle", "1:",
            "netem",
        ];
        let opts_str = netem_opts.join(" ");
        let words: Vec<&str> = opts_str.split_whitespace().collect();
        args.extend(words.iter().copied());
        tc(&args)?;
    }

    Ok(())
}

// ── IP address helper ─────────────────────────────────────────────────────────

fn get_ip(iface: &str) -> String {
    let out = Command::new("ip")
        .args(["-4", "addr", "show", iface])
        .output();

    if let Ok(out) = out {
        let text = String::from_utf8_lossy(&out.stdout);
        for line in text.lines() {
            let line = line.trim();
            if line.starts_with("inet ") {
                // e.g. "inet 192.168.1.42/24 brd ..."
                if let Some(addr) = line.split_whitespace().nth(1) {
                    return addr.split('/').next().unwrap_or("").to_string();
                }
            }
        }
    }
    "no IP".to_string()
}

// ── OLED display loop ─────────────────────────────────────────────────────────
//
// Runs in its own thread.  Every 2 s it reads the shared config and refreshes
// the display.  Layout (128×64):
//
//   Line 0:  eth0: 192.168.1.42
//   Line 1:  BW: 1000 kbps
//   Line 2:  Lat: 100ms  Jit: 10ms
//   Line 3:  Loss: 5.0%  Cor: 0%
//   Line 4:  [ACTIVE] / [OFF]
//
fn display_loop(shared: SharedConfig) {
    // Open I2C bus 1 (default on Pi GPIO pins 2/3)
    let i2c = match I2cdev::new("/dev/i2c-1") {
        Ok(d) => d,
        Err(e) => {
            eprintln!("OLED: cannot open I2C: {e}");
            return;
        }
    };

    let interface = I2CDisplayInterface::new(i2c);
    let mut display = Ssd1306::new(interface, DisplaySize128x64, DisplayRotation::Rotate0)
        .into_buffered_graphics_mode();

    if let Err(e) = display.init() {
        eprintln!("OLED: init failed: {e:?}");
        return;
    }

    let text_style = MonoTextStyleBuilder::new()
        .font(&FONT_6X10)
        .text_color(BinaryColor::On)
        .build();

    loop {
        let cfg = shared.lock().unwrap().clone();
        let ip = get_ip("eth0"); // always show primary port IP

        display.clear(BinaryColor::Off).ok();

        let lines: Vec<String> = vec![
            format!("eth0: {}", ip),
            if cfg.bandwidth_kbps > 0 {
                format!("BW:  {} kbps", cfg.bandwidth_kbps)
            } else {
                "BW:  unlimited".into()
            },
            format!("Lat: {}ms  Jit: {}ms", cfg.latency_ms, cfg.jitter_ms),
            format!("Loss:{:.0}%  Cor:{:.0}%", cfg.loss_pct, cfg.corrupt_pct),
            if cfg.active { "[  ACTIVE  ]".into() } else { "[   OFF    ]".into() },
        ];

        for (i, line) in lines.iter().enumerate() {
            Text::with_baseline(
                line,
                Point::new(0, (i as i32) * 12),
                text_style,
                Baseline::Top,
            )
            .draw(&mut display)
            .ok();
        }

        display.flush().ok();
        thread::sleep(Duration::from_secs(2));
    }
}

// ── HTTP server ───────────────────────────────────────────────────────────────

fn cors_headers() -> Vec<Header> {
    vec![
        Header::from_bytes("Access-Control-Allow-Origin", "*").unwrap(),
        Header::from_bytes("Access-Control-Allow-Methods", "GET, POST, OPTIONS").unwrap(),
        Header::from_bytes("Access-Control-Allow-Headers", "Content-Type").unwrap(),
        Header::from_bytes("Content-Type", "application/json").unwrap(),
    ]
}

fn json_response(body: &str, code: u16) -> Response<std::io::Cursor<Vec<u8>>> {
    let mut resp = Response::from_string(body).with_status_code(code);
    for h in cors_headers() {
        resp = resp.with_header(h);
    }
    resp
}

fn run_server(shared: SharedConfig) {
    let server = Server::http("0.0.0.0:8080").expect("Failed to bind port 8080");
    println!("HTTP API listening on :8080");

    for request in server.incoming_requests() {
        let method = request.method().as_str().to_uppercase();
        let url = request.url().to_string();

        // Handle CORS preflight
        if method == "OPTIONS" {
            let _ = request.respond(json_response("", 200));
            continue;
        }

        match (method.as_str(), url.as_str()) {
            // GET /config  — return current settings
            ("GET", "/config") => {
                let cfg = shared.lock().unwrap().clone();
                let body = serde_json::to_string(&cfg).unwrap();
                let _ = request.respond(json_response(&body, 200));
            }

            // POST /config  — update settings + re-apply
            ("POST", "/config") => {
                let mut content = String::new();
                let mut req = request;
                use std::io::Read;
                req.as_reader().read_to_string(&mut content).ok();

                match serde_json::from_str::<ShaperConfig>(&content) {
                    Ok(new_cfg) => {
                        let result = apply_config(&new_cfg);
                        match result {
                            Ok(_) => {
                                *shared.lock().unwrap() = new_cfg.clone();
                                let body = serde_json::to_string(&new_cfg).unwrap();
                                let _ = req.respond(json_response(&body, 200));
                            }
                            Err(e) => {
                                let body = format!("{{\"error\":\"{e}\"}}");
                                let _ = req.respond(json_response(&body, 500));
                            }
                        }
                    }
                    Err(e) => {
                        let body = format!("{{\"error\":\"bad JSON: {e}\"}}");
                        let _ = req.respond(json_response(&body, 400));
                    }
                }
            }

            // POST /reset  — clear all qdiscs, mark inactive
            ("POST", "/reset") => {
                let mut cfg = shared.lock().unwrap();
                cfg.active = false;
                clear_qdiscs(&cfg.interface);
                let body = serde_json::to_string(&*cfg).unwrap();
                let _ = request.respond(json_response(&body, 200));
            }

            // GET /ip  — return current IP of eth0
            ("GET", "/ip") => {
                let ip = get_ip("eth0");
                let body = format!("{{\"ip\":\"{ip}\"}}");
                let _ = request.respond(json_response(&body, 200));
            }

            _ => {
                let _ = request.respond(json_response("{\"error\":\"not found\"}", 404));
            }
        }
    }
}

// ── Entry point ───────────────────────────────────────────────────────────────

fn main() {
    let shared: SharedConfig = Arc::new(Mutex::new(ShaperConfig::default()));

    // Spawn OLED display thread
    let display_shared = Arc::clone(&shared);
    thread::spawn(move || display_loop(display_shared));

    // Run HTTP server on main thread
    run_server(shared);
}
