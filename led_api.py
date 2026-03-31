#!/usr/bin/env python3
"""
LincStation LED Dashboard — Backend API
Reverse-engineered I2C protocol for the LED controller at I2C address 0x26.
Protocol based on work by github.com/ffalt.

Run:  python3 led_api.py
Port: 50001
"""

import copy
import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ---------------------------------------------------------------------------
# Hardware constants
# ---------------------------------------------------------------------------

I2C_ADDR = 0x26

# LED BAR registers (front RGB strip)
LEDBAR_REG_MODE       = 0x90  # 0=solid, 1=breath, 2=loop
LEDBAR_REG_BRIGHTNESS = 0x91  # 0–255
LEDBAR_REG_COLOR_R    = 0x92  # solid / breath color
LEDBAR_REG_COLOR_G    = 0x93
LEDBAR_REG_COLOR_B    = 0x94
LEDBAR_REG_LOOP1_R    = 0x95  # loop — first color
LEDBAR_REG_LOOP1_G    = 0x96
LEDBAR_REG_LOOP1_B    = 0x97
LEDBAR_REG_LOOP2_R    = 0x98  # loop — second color
LEDBAR_REG_LOOP2_G    = 0x99
LEDBAR_REG_LOOP2_B    = 0x9A

# Power button LED registers
PWR_REG_ON    = 0xA0
PWR_REG_OFF   = 0xB0
PWR_MASK_WHITE = 0x01
PWR_MASK_RED   = 0x02
PWR_REG_BLINK  = 0x50
PWR_BLINK_ON   = 0x01
PWR_BLINK_OFF  = 0x00

# Drive bay strip definitions
# on_reg  : write here to turn LED on   (0xA0 for HDD/NET, 0xA1 for NVMe)
# off_reg : write here to turn LED off  (0xB0 / 0xB1, same relative mask)
# white_mask, red_mask : bit masks selecting the specific LED
# blink_reg : write 0x01 / 0x00 to enable / disable blinking
STRIPS = {
    "hdd0":  {"label": "HDD 0",  "on_reg": 0xA0, "off_reg": 0xB0, "white_mask": 0x04, "red_mask": 0x08, "blink_reg": 0x52},
    "hdd1":  {"label": "HDD 1",  "on_reg": 0xA0, "off_reg": 0xB0, "white_mask": 0x10, "red_mask": 0x20, "blink_reg": 0x54},
    "net":   {"label": "NET",    "on_reg": 0xA0, "off_reg": 0xB0, "white_mask": 0x40, "red_mask": 0x80, "blink_reg": 0x56},
    "nvme1": {"label": "NVMe 1", "on_reg": 0xA1, "off_reg": 0xB1, "white_mask": 0x01, "red_mask": 0x02, "blink_reg": 0x58},
    "nvme2": {"label": "NVMe 2", "on_reg": 0xA1, "off_reg": 0xB1, "white_mask": 0x04, "red_mask": 0x08, "blink_reg": 0x5A},
    "nvme3": {"label": "NVMe 3", "on_reg": 0xA1, "off_reg": 0xB1, "white_mask": 0x10, "red_mask": 0x20, "blink_reg": 0x5C},
    "nvme4": {"label": "NVMe 4", "on_reg": 0xA1, "off_reg": 0xB1, "white_mask": 0x40, "red_mask": 0x80, "blink_reg": 0x5E},
}

LEDBAR_MODES     = {0: "solid", 1: "breath", 2: "loop"}
LEDBAR_MODES_REV = {"solid": 0, "breath": 1, "loop": 2}


# ---------------------------------------------------------------------------
# Application state  — in-memory source of truth
# ---------------------------------------------------------------------------

state: dict = {
    "bus": None,
    "ledbar": {
        "mode":        "solid",
        "brightness":  200,
        "color":       [0, 200, 255],    # solid / breath RGB
        "loop_color1": [255, 50, 0],     # loop first color
        "loop_color2": [0, 100, 255],    # loop second color
    },
    "switch": {
        "color": "white",   # "white" | "red" | "off"
        "blink": False,
    },
    "strips": {sid: {"state": "off"} for sid in STRIPS},
}


# ---------------------------------------------------------------------------
# I2C helpers
# ---------------------------------------------------------------------------

def detect_i2c_bus() -> int | None:
    """Scan I2C buses 0–9 for the LED controller at address 0x26."""
    for bus in range(10):
        try:
            result = subprocess.run(
                ["i2cdetect", "-y", str(bus)],
                capture_output=True, text=True, timeout=3,
            )
            # i2cdetect marks detected addresses with their two-digit hex value
            if re.search(r"\b26\b", result.stdout):
                return bus
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            continue
    return None


def i2c_write(reg: int, value: int) -> bool:
    """Send one byte to the LED controller. Returns True on success."""
    bus = state["bus"]
    if bus is None:
        return False  # hardware not available — state is still updated
    try:
        subprocess.run(
            ["i2cset", "-y", str(bus), hex(I2C_ADDR), hex(reg), hex(value)],
            check=True, capture_output=True, timeout=2,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# LED control helpers
# ---------------------------------------------------------------------------

def apply_ledbar(lb: dict) -> None:
    """Push the full LED BAR configuration to hardware."""
    i2c_write(LEDBAR_REG_MODE,       LEDBAR_MODES_REV.get(lb["mode"], 0))
    i2c_write(LEDBAR_REG_BRIGHTNESS, int(lb["brightness"]))

    r,  g,  b  = lb["color"]
    r1, g1, b1 = lb["loop_color1"]
    r2, g2, b2 = lb["loop_color2"]

    i2c_write(LEDBAR_REG_COLOR_R, r);  i2c_write(LEDBAR_REG_COLOR_G, g);  i2c_write(LEDBAR_REG_COLOR_B, b)
    i2c_write(LEDBAR_REG_LOOP1_R, r1); i2c_write(LEDBAR_REG_LOOP1_G, g1); i2c_write(LEDBAR_REG_LOOP1_B, b1)
    i2c_write(LEDBAR_REG_LOOP2_R, r2); i2c_write(LEDBAR_REG_LOOP2_G, g2); i2c_write(LEDBAR_REG_LOOP2_B, b2)


def apply_switch(sw: dict) -> None:
    """Push power button LED state to hardware."""
    blink_val = PWR_BLINK_ON if sw.get("blink", False) else PWR_BLINK_OFF
    i2c_write(PWR_REG_BLINK, blink_val)

    color = sw.get("color", "off")
    if color == "white":
        i2c_write(PWR_REG_OFF, PWR_MASK_RED)    # ensure red is off first
        i2c_write(PWR_REG_ON,  PWR_MASK_WHITE)
    elif color == "red":
        i2c_write(PWR_REG_OFF, PWR_MASK_WHITE)  # ensure white is off first
        i2c_write(PWR_REG_ON,  PWR_MASK_RED)
    else:  # off
        i2c_write(PWR_REG_OFF, PWR_MASK_WHITE)
        i2c_write(PWR_REG_OFF, PWR_MASK_RED)


def apply_strip(strip_id: str, new_state: str) -> None:
    """Push a single bay strip state to hardware.

    States: 'off' | 'white' | 'red' | 'blink'
    'blink' enables white LED with the blink register.
    """
    s = STRIPS[strip_id]
    on_reg   = s["on_reg"]
    off_reg  = s["off_reg"]
    w_mask   = s["white_mask"]
    r_mask   = s["red_mask"]
    bl_reg   = s["blink_reg"]

    if new_state == "white":
        i2c_write(off_reg, r_mask)   # red off
        i2c_write(on_reg,  w_mask)   # white on
        i2c_write(bl_reg,  0x00)     # blink off
    elif new_state == "red":
        i2c_write(off_reg, w_mask)   # white off
        i2c_write(on_reg,  r_mask)   # red on
        i2c_write(bl_reg,  0x00)     # blink off
    elif new_state == "blink":
        i2c_write(off_reg, r_mask)   # red off
        i2c_write(on_reg,  w_mask)   # white on
        i2c_write(bl_reg,  0x01)     # blink on
    else:  # off
        i2c_write(off_reg, w_mask)
        i2c_write(off_reg, r_mask)
        i2c_write(bl_reg,  0x00)


def all_leds_off() -> None:
    """Turn every LED off and update the in-memory state."""
    # LED BAR: keep settings, set brightness to 0
    i2c_write(LEDBAR_REG_BRIGHTNESS, 0)
    state["ledbar"]["brightness"] = 0

    # Power button
    i2c_write(PWR_REG_OFF,  PWR_MASK_WHITE)
    i2c_write(PWR_REG_OFF,  PWR_MASK_RED)
    i2c_write(PWR_REG_BLINK, PWR_BLINK_OFF)
    state["switch"] = {"color": "off", "blink": False}

    # All bay strips
    for sid in STRIPS:
        apply_strip(sid, "off")
        state["strips"][sid]["state"] = "off"


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------

def get_system_stats() -> dict:
    """Return CPU, RAM, disk and network metrics via psutil."""
    if not HAS_PSUTIL:
        return {"available": False}

    vm  = psutil.virtual_memory()
    nio = psutil.net_io_counters()

    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mountpoint": part.mountpoint,
                "device":     part.device,
                "fstype":     part.fstype,
                "total_gb":   round(u.total / 1e9, 2),
                "used_gb":    round(u.used  / 1e9, 2),
                "percent":    round(u.percent, 1),
            })
        except PermissionError:
            continue

    return {
        "available":  True,
        "cpu":        round(psutil.cpu_percent(interval=None), 1),
        "ram": {
            "percent":  round(vm.percent, 1),
            "used_gb":  round(vm.used  / 1e9, 2),
            "total_gb": round(vm.total / 1e9, 2),
        },
        "disks": disks,
        "net": {
            "bytes_sent": nio.bytes_sent,
            "bytes_recv": nio.bytes_recv,
        },
    }


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

STATIC_DIR   = Path(__file__).parent
PRESETS_FILE = STATIC_DIR / "presets.json"

# Presets — persisted to presets.json, loaded at startup
presets: dict = {}

def _load_presets() -> None:
    global presets
    if PRESETS_FILE.exists():
        try:
            presets = json.loads(PRESETS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            presets = {}

def _save_presets() -> None:
    PRESETS_FILE.write_text(json.dumps(presets, indent=2))

app = Flask(__name__, static_folder=str(STATIC_DIR))


@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "led_dashboard.html")


@app.route("/api/state", methods=["GET"])
def api_state():
    """Full current LED state + strip metadata for the frontend."""
    return jsonify({
        **copy.deepcopy(state),
        "strips_meta": {k: v["label"] for k, v in STRIPS.items()},
    })


@app.route("/api/status", methods=["GET"])
def api_status():
    """System stats + LED state — polled every 5 s by the frontend."""
    return jsonify({
        "system": get_system_stats(),
        "state":  copy.deepcopy(state),
    })


@app.route("/api/ledbar", methods=["POST"])
def api_ledbar():
    """Update LED BAR configuration.

    Body (all fields optional):
      mode         : "solid" | "breath" | "loop"
      brightness   : 0–255
      color        : [R, G, B]   — solid/breath color
      loop_color1  : [R, G, B]
      loop_color2  : [R, G, B]
    """
    data = request.get_json(force=True) or {}
    lb   = state["ledbar"]

    if "mode" in data and data["mode"] in LEDBAR_MODES_REV:
        lb["mode"] = data["mode"]
    if "brightness" in data:
        lb["brightness"] = max(0, min(255, int(data["brightness"])))
    for key in ("color", "loop_color1", "loop_color2"):
        if key in data and len(data[key]) == 3:
            lb[key] = [max(0, min(255, int(c))) for c in data[key]]

    apply_ledbar(lb)
    return jsonify({"ok": True, "ledbar": lb})


@app.route("/api/switch", methods=["POST"])
def api_switch():
    """Control power button LED.

    Body:
      color : "white" | "red" | "off"
      blink : true | false
    """
    data = request.get_json(force=True) or {}
    sw   = state["switch"]

    if "color" in data and data["color"] in ("white", "red", "off"):
        sw["color"] = data["color"]
    if "blink" in data:
        sw["blink"] = bool(data["blink"])

    apply_switch(sw)
    return jsonify({"ok": True, "switch": sw})


@app.route("/api/strip/<strip_id>", methods=["POST"])
def api_strip(strip_id: str):
    """Control a single bay LED strip.

    strip_id : hdd0 | hdd1 | net | nvme1..nvme4
    Body:
      state : "off" | "white" | "red" | "blink"
    """
    if strip_id not in STRIPS:
        return jsonify({"ok": False, "error": f"Unknown strip: {strip_id}"}), 400

    data      = request.get_json(force=True) or {}
    new_state = data.get("state", "off")

    if new_state not in ("off", "white", "red", "blink"):
        return jsonify({"ok": False, "error": f"Invalid state: {new_state}"}), 400

    apply_strip(strip_id, new_state)
    state["strips"][strip_id]["state"] = new_state
    return jsonify({"ok": True, "strip": strip_id, "state": new_state})


@app.route("/api/all/off", methods=["POST"])
def api_all_off():
    """Turn off every LED on the device."""
    all_leds_off()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

@app.route("/api/preset", methods=["GET"])
def api_preset_list():
    """List all saved preset names."""
    return jsonify({"presets": list(presets.keys())})


@app.route("/api/preset/<name>", methods=["POST", "DELETE"])
def api_preset(name: str):
    """POST: save current LED state as a named preset.
       DELETE: remove a preset.
    """
    if request.method == "DELETE":
        if name not in presets:
            return jsonify({"ok": False, "error": "Not found"}), 404
        del presets[name]
        _save_presets()
        return jsonify({"ok": True})

    # POST — snapshot current state
    presets[name] = {
        "ledbar": copy.deepcopy(state["ledbar"]),
        "switch": copy.deepcopy(state["switch"]),
        "strips": copy.deepcopy(state["strips"]),
    }
    _save_presets()
    return jsonify({"ok": True, "name": name})


@app.route("/api/preset/<name>/apply", methods=["POST"])
def api_preset_apply(name: str):
    """Apply a saved preset to hardware and update state."""
    if name not in presets:
        return jsonify({"ok": False, "error": "Not found"}), 404
    p = presets[name]
    if "ledbar" in p:
        state["ledbar"].update(p["ledbar"])
        apply_ledbar(state["ledbar"])
    if "switch" in p:
        state["switch"].update(p["switch"])
        apply_switch(state["switch"])
    if "strips" in p:
        for sid, v in p["strips"].items():
            if sid in STRIPS:
                apply_strip(sid, v["state"])
                state["strips"][sid] = copy.deepcopy(v)
    return jsonify({"ok": True, "name": name, "state": copy.deepcopy(state)})


# ---------------------------------------------------------------------------
# Extension point — add new routes here:
#   /api/animation/start  — background strip animations tied to metrics
#   /api/schedule         — cron-based LED automation
#   /api/sync             — read hardware state back into memory
# ---------------------------------------------------------------------------


# =============================================================================
# Animation System
# =============================================================================

# Physical left-to-right strip order (matches front panel layout)
STRIP_ORDER = ["hdd0", "hdd1", "nvme1", "nvme2", "nvme3", "nvme4", "net"]


# ---------------------------------------------------------------------------
# Animation frame generators
# Each returns a list of {"states": {strip_id: state}, "delay_ms": int}
# ---------------------------------------------------------------------------

def _anim_sweep(p: dict, targets: set) -> list:
    """Moving light — good for network traffic direction."""
    color   = p.get("color", "white")
    rtl     = p.get("direction", "ltr") == "rtl"
    speed   = int(p.get("speed_ms", 150))
    tail    = max(0, int(p.get("tail", 1)))
    ordered = [s for s in (STRIP_ORDER[::-1] if rtl else STRIP_ORDER) if s in targets]
    frames  = []
    for i, lead in enumerate(ordered):
        frame = {s: "off" for s in targets}
        frame[lead] = color
        for t in range(1, tail + 1):
            if i - t >= 0:
                frame[ordered[i - t]] = color
        frames.append({"states": frame, "delay_ms": speed})
    frames.append({"states": {s: "off" for s in targets}, "delay_ms": speed})
    return frames


def _anim_chase(p: dict, targets: set) -> list:
    """Single lit strip bouncing back and forth (Cylon)."""
    color   = p.get("color", "white")
    speed   = int(p.get("speed_ms", 200))
    ordered = [s for s in STRIP_ORDER if s in targets]
    path    = ordered + list(reversed(ordered[1:-1])) if len(ordered) > 2 else ordered
    return [{"states": {**{s: "off" for s in targets}, **{step: color}}, "delay_ms": speed}
            for step in path]


def _anim_fill(p: dict, targets: set) -> list:
    """Fill N% of strips — useful for static percentage display."""
    color   = p.get("color", "white")
    speed   = int(p.get("speed_ms", 100))
    pct     = max(0.0, min(100.0, float(p.get("fill_pct", 100))))
    ordered = [s for s in STRIP_ORDER if s in targets]
    n       = max(0, round(len(ordered) * pct / 100))
    states  = {s: (color if i < n else "off") for i, s in enumerate(ordered)}
    return [{"states": states, "delay_ms": speed}]


def _anim_flash(p: dict, targets: set) -> list:
    """All targets flash simultaneously."""
    color  = p.get("color", "red")
    on_ms  = int(p.get("on_ms", 200))
    off_ms = int(p.get("off_ms", 200))
    return [
        {"states": {s: color for s in targets}, "delay_ms": on_ms},
        {"states": {s: "off"  for s in targets}, "delay_ms": off_ms},
    ]


def _anim_breathe(p: dict, targets: set) -> list:
    """All targets blink slowly in unison."""
    color  = p.get("color", "white")
    on_ms  = int(p.get("on_ms", 800))
    off_ms = int(p.get("off_ms", 800))
    return [
        {"states": {s: color for s in targets}, "delay_ms": on_ms},
        {"states": {s: "off"  for s in targets}, "delay_ms": off_ms},
    ]


def _anim_wave(p: dict, targets: set) -> list:
    """Staggered blink offset strip by strip."""
    color   = p.get("color", "white")
    speed   = int(p.get("speed_ms", 100))
    step    = max(1, int(p.get("step", 2)))
    ordered = [s for s in STRIP_ORDER if s in targets]
    return [
        {"states": {s: (color if i % step == offset else "off")
                    for i, s in enumerate(ordered)}, "delay_ms": speed}
        for offset in range(step)
    ]


def _anim_alternate(p: dict, targets: set) -> list:
    """Even/odd strips swap between two colors."""
    ca     = p.get("color_a", "white")
    cb     = p.get("color_b", "red")
    speed  = int(p.get("speed_ms", 400))
    ord_   = [s for s in STRIP_ORDER if s in targets]
    fa     = {s: (ca if i % 2 == 0 else cb) for i, s in enumerate(ord_)}
    fb     = {s: (cb if i % 2 == 0 else ca) for i, s in enumerate(ord_)}
    return [{"states": fa, "delay_ms": speed}, {"states": fb, "delay_ms": speed}]


def _anim_sparkle(p: dict, targets: set) -> list:
    """Short random-looking single-strip flickers."""
    import random
    color  = p.get("color", "white")
    on_ms  = int(p.get("on_ms", 80))
    off_ms = int(p.get("off_ms", 220))
    lst    = list(targets); random.shuffle(lst)
    frames = []
    for s in lst:
        frames.append({"states": {t: (color if t == s else "off") for t in targets}, "delay_ms": on_ms})
        frames.append({"states": {t: "off" for t in targets}, "delay_ms": off_ms})
    return frames


# ---------------------------------------------------------------------------
# LED BAR animation generators
# Frames use {"ledbar": {mode, brightness, color, ...}, "delay_ms": N}
# The chip handles smooth animation internally for breath/loop modes.
# ---------------------------------------------------------------------------

def _ledbar_solid(p: dict, _targets: set) -> list:
    """Set LED BAR to a solid color."""
    return [{"ledbar": {
        "mode": "solid",
        "brightness": int(p.get("brightness", 200)),
        "color": p.get("color", [0, 200, 255]),
    }, "delay_ms": 500}]


def _ledbar_breath(p: dict, _targets: set) -> list:
    """Set LED BAR to breath mode (chip-driven)."""
    return [{"ledbar": {
        "mode": "breath",
        "brightness": int(p.get("brightness", 200)),
        "color": p.get("color", [0, 200, 255]),
    }, "delay_ms": 500}]


def _ledbar_loop(p: dict, _targets: set) -> list:
    """Set LED BAR to loop/crossfade mode (chip-driven)."""
    return [{"ledbar": {
        "mode": "loop",
        "brightness": int(p.get("brightness", 200)),
        "loop_color1": p.get("loop_color1", [255, 50, 0]),
        "loop_color2": p.get("loop_color2", [0, 100, 255]),
    }, "delay_ms": 500}]


def _ledbar_flash(p: dict, _targets: set) -> list:
    """Software strobe: alternates brightness on/off rapidly."""
    color  = p.get("color", [255, 0, 0])
    bright = int(p.get("brightness", 255))
    on_ms  = int(p.get("on_ms", 200))
    off_ms = int(p.get("off_ms", 200))
    return [
        {"ledbar": {"mode": "solid", "brightness": bright, "color": color}, "delay_ms": on_ms},
        {"ledbar": {"mode": "solid", "brightness": 0,      "color": color}, "delay_ms": off_ms},
    ]


# Registry: name → {fn, defaults}
# Prefix "ledbar_" marks LED BAR targets; all others target strips.
ANIMATION_LIBRARY: dict = {
    # ── Strip animations ──────────────────────────────────
    "sweep":          {"fn": _anim_sweep,    "defaults": {"direction": "ltr", "color": "white", "speed_ms": 150, "tail": 1}},
    "chase":          {"fn": _anim_chase,    "defaults": {"color": "white", "speed_ms": 200}},
    "fill":           {"fn": _anim_fill,     "defaults": {"color": "white", "speed_ms": 100, "fill_pct": 100}},
    "flash":          {"fn": _anim_flash,    "defaults": {"color": "red",   "on_ms": 200, "off_ms": 200}},
    "breathe":        {"fn": _anim_breathe,  "defaults": {"color": "white", "on_ms": 800, "off_ms": 800}},
    "wave":           {"fn": _anim_wave,     "defaults": {"color": "white", "speed_ms": 100, "step": 2}},
    "alternate":      {"fn": _anim_alternate,"defaults": {"color_a": "white", "color_b": "red", "speed_ms": 400}},
    "sparkle":        {"fn": _anim_sparkle,  "defaults": {"color": "white", "on_ms": 80,  "off_ms": 220}},
    # ── LED BAR animations ────────────────────────────────
    "ledbar_solid":   {"fn": _ledbar_solid,  "defaults": {"brightness": 200, "color": [0, 200, 255]}},
    "ledbar_breath":  {"fn": _ledbar_breath, "defaults": {"brightness": 200, "color": [0, 200, 255]}},
    "ledbar_loop":    {"fn": _ledbar_loop,   "defaults": {"brightness": 200, "loop_color1": [255, 50, 0], "loop_color2": [0, 100, 255]}},
    "ledbar_flash":   {"fn": _ledbar_flash,  "defaults": {"brightness": 255, "color": [255, 0, 0], "on_ms": 200, "off_ms": 200}},
}


# ---------------------------------------------------------------------------
# AnimationRunner
# ---------------------------------------------------------------------------

class AnimationRunner:
    """
    Runs multiple animations concurrently on non-overlapping strip subsets.
    Conflict resolution: per-strip, highest priority wins.
    Loop granularity: 10 ms (~0.01 % CPU impact).
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._tasks:  dict = {}   # event_id → task
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="anim-runner")
        self._running = False

    def start(self):
        self._running = True
        self._thread.start()

    def stop(self):
        self._running = False

    # ── Public API ──────────────────────────────────────────

    def play(self, event_id: str, animation: str, params: dict,
             targets: list, priority: int, on_exit: dict) -> bool:
        defn = ANIMATION_LIBRARY.get(animation)
        if not defn:
            return False
        # "ledbar" is a virtual target; strip targets must exist in STRIPS
        valid_strip_targets = set(targets) & set(STRIPS.keys())
        is_ledbar = "ledbar" in targets
        target_set = valid_strip_targets | ({"ledbar"} if is_ledbar else set())
        if not target_set:
            return False
        merged = {**defn["defaults"], **params}
        frames = defn["fn"](merged, valid_strip_targets or {"ledbar"})
        if not frames:
            return False
        task = {
            "id":        event_id,
            "animation": animation,
            "params":    merged,
            "targets":   target_set,
            "priority":  priority,
            "on_exit":   on_exit,
            "frames":    frames,
            "frame_idx": 0,
            "next_tick": time.monotonic(),
        }
        # Snapshot current LED BAR state so on_exit can restore it
        if is_ledbar:
            task["prev_ledbar"] = copy.deepcopy(state["ledbar"])
        with self._lock:
            self._tasks[event_id] = task
        return True

    def stop_event(self, event_id: str):
        with self._lock:
            task = self._tasks.pop(event_id, None)
        if task:
            self._do_on_exit(task)

    def stop_all(self):
        with self._lock:
            tasks, self._tasks = list(self._tasks.values()), {}
        for task in tasks:
            self._do_on_exit(task)

    def get_status(self) -> list:
        with self._lock:
            return [
                {"id": k, "animation": v["animation"],
                 "targets": sorted(v["targets"]), "priority": v["priority"]}
                for k, v in self._tasks.items()
            ]

    # ── on_exit ─────────────────────────────────────────────

    def _do_on_exit(self, task: dict):
        oe   = task.get("on_exit", {})
        kind = oe.get("type", "off")

        # ── LED BAR: always restore the snapshot taken at play() time ──
        if "ledbar" in task["targets"]:
            prev = task.get("prev_ledbar")
            if prev:
                state["ledbar"].update(prev)
                apply_ledbar(state["ledbar"])
            # Additionally apply preset's ledbar component if requested
            if kind == "preset":
                lb = presets.get(oe.get("name", ""), {}).get("ledbar")
                if lb:
                    state["ledbar"].update(lb)
                    apply_ledbar(state["ledbar"])

        # ── Strips ────────────────────────────────────────────────────
        strip_targets = {s for s in task["targets"] if s != "ledbar"}
        if not strip_targets:
            return
        if kind == "static":
            st = oe.get("state", "off")
            for sid in strip_targets:
                apply_strip(sid, st); state["strips"][sid]["state"] = st
        elif kind == "preset":
            name = oe.get("name", "")
            for sid in strip_targets:
                v  = presets.get(name, {}).get("strips", {}).get(sid)
                st = v["state"] if v else "off"
                apply_strip(sid, st); state["strips"][sid]["state"] = st
        else:   # "off"
            for sid in strip_targets:
                apply_strip(sid, "off"); state["strips"][sid]["state"] = "off"

    # ── Runner loop ─────────────────────────────────────────

    def _loop(self):
        while self._running:
            now = time.monotonic()
            with self._lock:
                due = [t for t in self._tasks.values() if now >= t["next_tick"]]

            if due:
                # Per-strip: highest-priority task wins
                winner: dict = {}   # strip_id → task
                for task in due:
                    for sid in task["targets"]:
                        if sid not in winner or task["priority"] > winner[sid]["priority"]:
                            winner[sid] = task

                # Group strips back by winning task
                by_task: dict = {}
                for sid, task in winner.items():
                    by_task.setdefault(task["id"], (task, set()))[1].add(sid)

                for task, won_targets in by_task.values():
                    frame = task["frames"][task["frame_idx"]]
                    # Apply strip states
                    for sid, st in frame.get("states", {}).items():
                        if sid in won_targets:
                            apply_strip(sid, st)
                            state["strips"][sid]["state"] = st
                    # Apply LED BAR state (chip handles animation internally)
                    if "ledbar" in frame and "ledbar" in won_targets:
                        state["ledbar"].update(frame["ledbar"])
                        apply_ledbar(state["ledbar"])
                    task["frame_idx"] = (task["frame_idx"] + 1) % len(task["frames"])
                    task["next_tick"] = now + frame.get("delay_ms", 100) / 1000.0

            time.sleep(0.010)


# ---------------------------------------------------------------------------
# MetricPoller
# ---------------------------------------------------------------------------

POLLER_DEFAULTS: dict = {
    "enabled":     True,
    "interval_ms": 2000,
    "thresholds": {
        "net_in_mbps":        5.0,    # MB/s inbound
        "net_out_mbps":       5.0,    # MB/s outbound
        "cpu_percent":        80.0,
        "cpu_sustained_secs": 10.0,   # 0 = instant
        "ram_percent":        85.0,
        "disk_io_mbps":       50.0,   # aggregate read+write
    },
}


class MetricPoller:
    """
    Lightweight periodic metrics collector.
    Non-blocking psutil calls + self-correcting sleep = minimal NAS impact.
    """

    def __init__(self, config: dict, callback):
        self.config  = copy.deepcopy(config)
        self._cb     = callback
        self._thread = threading.Thread(target=self._loop, daemon=True, name="metric-poller")
        self._running    = False
        self._prev_net   = None
        self._prev_disk  = None
        self._prev_time: float | None = None

    def start(self):
        if not HAS_PSUTIL:
            return
        psutil.cpu_percent(interval=None)   # warm-up: first call always returns 0.0
        self._running = True
        self._thread.start()

    def stop(self):
        self._running = False

    def update_config(self, patch: dict):
        self.config.update({k: v for k, v in patch.items() if k != "thresholds"})
        if "thresholds" in patch:
            self.config.setdefault("thresholds", {}).update(patch["thresholds"])

    def _loop(self):
        while self._running:
            t0       = time.monotonic()
            interval = max(0.5, self.config.get("interval_ms", 2000) / 1000.0)

            if self.config.get("enabled", True):
                m = self._collect(t0)
                if m:
                    try:
                        self._cb(m)
                    except Exception:
                        pass

            time.sleep(max(0.0, interval - (time.monotonic() - t0)))

    def _collect(self, now: float) -> dict | None:
        try:
            cpu  = psutil.cpu_percent(interval=None)
            vm   = psutil.virtual_memory()
            net  = psutil.net_io_counters()
            disk = psutil.disk_io_counters(perdisk=False)

            net_in = net_out = disk_io = 0.0
            if self._prev_net and self._prev_time:
                dt      = max(0.001, now - self._prev_time)
                net_in  = max(0.0, (net.bytes_recv - self._prev_net.bytes_recv)  / dt / 1e6)
                net_out = max(0.0, (net.bytes_sent - self._prev_net.bytes_sent)  / dt / 1e6)
                if disk and self._prev_disk:
                    disk_io = max(0.0, (
                        (disk.read_bytes + disk.write_bytes) -
                        (self._prev_disk.read_bytes + self._prev_disk.write_bytes)
                    ) / dt / 1e6)

            self._prev_net  = net
            self._prev_disk = disk
            self._prev_time = now
            return {"cpu": cpu, "ram": vm.percent,
                    "net_in_mbps": net_in, "net_out_mbps": net_out,
                    "disk_io_mbps": disk_io, "timestamp": now}
        except Exception:
            return None


# ---------------------------------------------------------------------------
# EventEngine
# ---------------------------------------------------------------------------

class EventEngine:
    """
    Evaluates metric thresholds, emits enter/exit events,
    and drives AnimationRunner through the binding table.
    """

    KNOWN_EVENTS = [
        "net_in.spike", "net_out.spike",
        "cpu.high", "ram.high", "disk.active", "idle",
    ]

    def __init__(self, runner: AnimationRunner):
        self._runner          = runner
        self._active:  set    = set()
        self._sus_since: dict = {}   # event → monotonic timestamp

    def on_metrics(self, metrics: dict):
        thr     = poller_config.get("thresholds", POLLER_DEFAULTS["thresholds"])
        now     = metrics["timestamp"]
        current: set = set()

        if metrics["net_in_mbps"]  >= thr.get("net_in_mbps",  5):   current.add("net_in.spike")
        if metrics["net_out_mbps"] >= thr.get("net_out_mbps", 5):   current.add("net_out.spike")
        if metrics["ram"]          >= thr.get("ram_percent",  85):  current.add("ram.high")
        if metrics["disk_io_mbps"] >= thr.get("disk_io_mbps", 50):  current.add("disk.active")

        # CPU with optional sustain window
        sus = thr.get("cpu_sustained_secs", 0)
        if metrics["cpu"] >= thr.get("cpu_percent", 80):
            if sus <= 0:
                current.add("cpu.high")
            else:
                since = self._sus_since.setdefault("cpu.high", now)
                if now - since >= sus:
                    current.add("cpu.high")
        else:
            self._sus_since.pop("cpu.high", None)

        if not current:
            current.add("idle")

        entered = current - self._active
        exited  = self._active - current

        snap = bindings[:]   # shallow copy — safe from concurrent API writes
        for ev in exited:
            for b in snap:
                if b.get("event") == ev and b.get("enabled", True):
                    self._runner.stop_event(b["id"])

        for ev in entered:
            for b in snap:
                if b.get("event") == ev and b.get("enabled", True):
                    self._runner.play(
                        event_id  = b["id"],
                        animation = b["animation"],
                        params    = b.get("params", {}),
                        targets   = b.get("targets", list(STRIPS.keys())),
                        priority  = b.get("priority", 5),
                        on_exit   = b.get("on_exit", {"type": "off"}),
                    )
        self._active = current


# ---------------------------------------------------------------------------
# Automation config persistence
# ---------------------------------------------------------------------------

POLLER_CONFIG_FILE = STATIC_DIR / "poller_config.json"
BINDINGS_FILE      = STATIC_DIR / "bindings.json"

poller_config: dict = copy.deepcopy(POLLER_DEFAULTS)
bindings: list      = []   # mutated in-place everywhere — EventEngine reads via bindings[:]


def _load_automation_config() -> None:
    global poller_config
    if POLLER_CONFIG_FILE.exists():
        try:
            poller_config = json.loads(POLLER_CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    if BINDINGS_FILE.exists():
        try:
            loaded = json.loads(BINDINGS_FILE.read_text())
            bindings.clear()
            bindings.extend(loaded)
        except (json.JSONDecodeError, OSError):
            pass


def _save_poller_config() -> None:
    POLLER_CONFIG_FILE.write_text(json.dumps(poller_config, indent=2))


def _save_bindings() -> None:
    BINDINGS_FILE.write_text(json.dumps(bindings, indent=2))


def _maybe_start_binding(b: dict) -> None:
    """Start a binding's animation immediately if its event is currently active."""
    if not animation_runner or not event_engine:
        return
    if b.get("enabled", True) and b.get("event") in event_engine._active:
        animation_runner.play(
            event_id  = b["id"],
            animation = b["animation"],
            params    = b.get("params", {}),
            targets   = b.get("targets", list(STRIPS.keys())),
            priority  = b.get("priority", 5),
            on_exit   = b.get("on_exit", {"type": "off"}),
        )


# Global instances — created in __main__
animation_runner: AnimationRunner | None = None
metric_poller:    MetricPoller    | None = None
event_engine:     EventEngine     | None = None


# ---------------------------------------------------------------------------
# Automation routes
# ---------------------------------------------------------------------------

@app.route("/api/automation/status", methods=["GET"])
def api_automation_status():
    """Active animations + current event set + poller state."""
    return jsonify({
        "active": animation_runner.get_status() if animation_runner else [],
        "events": list(event_engine._active)    if event_engine    else [],
        "poller": {
            "enabled":     poller_config.get("enabled", True),
            "interval_ms": poller_config.get("interval_ms", 2000),
        },
    })


@app.route("/api/poller/config", methods=["GET", "POST"])
def api_poller_config_route():
    """GET full config, POST to patch any field."""
    if request.method == "GET":
        return jsonify(poller_config)
    patch = request.get_json(force=True) or {}
    if "interval_ms" in patch:
        poller_config["interval_ms"] = max(500, int(patch["interval_ms"]))
    if "enabled" in patch:
        poller_config["enabled"] = bool(patch["enabled"])
    if "thresholds" in patch and isinstance(patch["thresholds"], dict):
        poller_config.setdefault("thresholds", {}).update(
            {k: float(v) for k, v in patch["thresholds"].items()}
        )
    _save_poller_config()
    if metric_poller:
        metric_poller.update_config(poller_config)
    return jsonify({"ok": True, "config": poller_config})


@app.route("/api/binding", methods=["GET", "POST"])
def api_binding_collection():
    if request.method == "GET":
        return jsonify({
            "bindings":   bindings,
            "animations": {k: {"defaults": v["defaults"]} for k, v in ANIMATION_LIBRARY.items()},
            "events":     EventEngine.KNOWN_EVENTS,
            "strips":     list(STRIPS.keys()) + ["ledbar"],
        })
    data = request.get_json(force=True) or {}
    if not data.get("event") or not data.get("animation"):
        return jsonify({"ok": False, "error": "event and animation are required"}), 400
    if data["animation"] not in ANIMATION_LIBRARY:
        return jsonify({"ok": False, "error": f"Unknown animation: {data['animation']}"}), 400

    binding = {
        "id":        data.get("id") or str(uuid.uuid4())[:8],
        "label":     data.get("label", ""),
        "event":     data["event"],
        "animation": data["animation"],
        "params":    data.get("params", {}),
        "targets":   data.get("targets", list(STRIPS.keys())),
        "priority":  int(data.get("priority", 5)),
        "on_exit":   data.get("on_exit", {"type": "off"}),
        "enabled":   bool(data.get("enabled", True)),
    }
    bindings.append(binding)
    _save_bindings()
    # If this event is already active, start the animation immediately
    _maybe_start_binding(binding)
    return jsonify({"ok": True, "binding": binding}), 201


@app.route("/api/binding/<binding_id>", methods=["PUT", "DELETE"])
def api_binding_item(binding_id: str):
    idx = next((i for i, b in enumerate(bindings) if b["id"] == binding_id), None)
    if idx is None:
        return jsonify({"ok": False, "error": "Not found"}), 404

    if request.method == "DELETE":
        if animation_runner:
            animation_runner.stop_event(binding_id)
        bindings.pop(idx)
        _save_bindings()
        return jsonify({"ok": True})

    # PUT — update provided fields
    data = request.get_json(force=True) or {}
    b    = bindings[idx]
    for field in ("label", "event", "animation", "params", "targets",
                  "priority", "on_exit", "enabled"):
        if field in data:
            b[field] = data[field]
    _save_bindings()
    # Stop old instance unconditionally, then restart if event is currently active
    if animation_runner:
        animation_runner.stop_event(binding_id)
    if b.get("enabled", True):
        _maybe_start_binding(b)
    return jsonify({"ok": True, "binding": b})


@app.route("/api/animation/stop", methods=["POST"])
def api_animation_stop_all():
    """Immediately stop all running animations."""
    if animation_runner:
        animation_runner.stop_all()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("LincStation LED Dashboard")
    print("=" * 40)

    _load_presets()
    _load_automation_config()

    bus = detect_i2c_bus()
    if bus is not None:
        state["bus"] = bus
        print(f"  I2C bus   : {bus}  (device 0x{I2C_ADDR:02X})")
    else:
        print("  WARNING   : I2C bus not found — hardware commands are no-ops.")
        print("              Ensure i2c-tools is installed and run as root.")

    animation_runner = AnimationRunner()
    animation_runner.start()

    event_engine = EventEngine(animation_runner)

    metric_poller = MetricPoller(poller_config, event_engine.on_metrics)
    metric_poller.start()

    print(f"  Animations: {len(ANIMATION_LIBRARY)} disponibles")
    print(f"  Bindings  : {len(bindings)} chargés")
    print(f"  Poller    : {poller_config['interval_ms']} ms  "
          f"({'actif' if poller_config.get('enabled') else 'inactif'})")
    print(f"  URL       : http://0.0.0.0:50001")
    print()
    app.run(host="0.0.0.0", port=50001, debug=False)
