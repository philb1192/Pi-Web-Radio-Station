#!/usr/bin/env python3
"""
Bluetooth pairing agent + audio routing for the radio server.

Behaviours:
  - Auto-accepts all pairing requests (NoInputNoOutput).
  - Keeps the adapter powered, discoverable, and pairable.
  - When a phone connects: pauses internet radio.
  - Routes phone audio (A2DP) to the speaker via a PulseAudio loopback.
  - When the phone disconnects: resumes radio if it was playing.
  - If no Bluetooth audio plays for 15 minutes: disconnects the phone.
"""

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bt-agent] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bt-agent")

AGENT_PATH = "/com/radio/agent"
AGENT_CAPABILITY = "NoInputNoOutput"
ALSA_SINK = "alsa_output.hw_0_0"
RADIO_URL = "http://localhost:8080/api"
IDLE_TIMEOUT_SECS = 30 * 60

PULSE_ENV = {
    **os.environ,
    "PULSE_RUNTIME_PATH": "/run/user/1000/pulse",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}

active_loopbacks = {}  # mac -> PA module index
device_state = {}      # mac -> {was_playing, station_id, connected_at, last_active}
state_lock = threading.Lock()
_bus = None


# ---------------------------------------------------------------------------
# Bluetooth adapter
# ---------------------------------------------------------------------------

def bt_cmd(*args):
    subprocess.run(["bluetoothctl", *args], capture_output=True)


def setup_adapter():
    time.sleep(2)
    bt_cmd("power", "on")
    bt_cmd("discoverable", "on")
    bt_cmd("pairable", "on")
    log.info("Adapter: powered on, discoverable, pairable")


# ---------------------------------------------------------------------------
# PulseAudio helpers
# ---------------------------------------------------------------------------

def pa_run(*args):
    try:
        result = subprocess.run(
            ["runuser", "-u", "pi", "--", "pactl", *args],
            capture_output=True, text=True, env=PULSE_ENV,
        )
        return result.stdout.strip()
    except Exception as e:
        log.warning(f"pa_run({args}): {e}")
        return ""


def find_bt_source(mac):
    mac_norm = mac.replace(":", "_").upper()
    for line in pa_run("list", "short", "sources").splitlines():
        if mac_norm in line.upper() and "monitor" not in line.lower():
            return line.split()[1]
    return None


def get_bt_source_state(mac):
    """Return 'RUNNING', 'IDLE', 'SUSPENDED', or None.

    pactl list sources output per source block:
        Source #N
            State: <VALUE>      ← State comes BEFORE Name
            Name: bluez_source.XX_XX_XX...

    We must find the block containing the MAC, then read State from it.
    """
    mac_norm = mac.replace(":", "_").upper()
    output = pa_run("list", "sources")
    if not output:
        return None

    # Split into per-source blocks on "Source #" headers
    blocks = []
    current: list[str] = []
    for line in output.splitlines():
        if line.startswith("Source #"):
            if current:
                blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    for block in blocks:
        block_text = "\n".join(block)
        if mac_norm not in block_text.upper():
            continue
        for line in block:
            stripped = line.strip()
            if stripped.startswith("State:"):
                return stripped.split("State:", 1)[1].strip()

    return None


def load_loopback(source_name):
    out = pa_run(
        "load-module", "module-loopback",
        f"source={source_name}",
        f"sink={ALSA_SINK}",
        "latency_msec=50",
        "source_dont_move=true",
        "sink_dont_move=true",
    )
    try:
        return int(out)
    except (ValueError, TypeError):
        return None


def unload_loopback(module_index):
    pa_run("unload-module", str(module_index))


# ---------------------------------------------------------------------------
# Radio server API
# ---------------------------------------------------------------------------

def radio_get(path):
    try:
        with urllib.request.urlopen(f"{RADIO_URL}/{path}", timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"Radio GET {path}: {e}")
        return None


def radio_post(path, data=None):
    try:
        body = json.dumps(data).encode() if data is not None else b""
        req = urllib.request.Request(
            f"{RADIO_URL}/{path}", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"Radio POST {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Idle monitor — background thread
# ---------------------------------------------------------------------------

def idle_monitor():
    while True:
        try:
            time.sleep(30)
            now = time.time()
            to_disconnect = []
            status_updates = []  # (mac, streaming, idle_secs) — collected under lock, posted after

            with state_lock:
                for mac, state in device_state.items():
                    src_state = get_bt_source_state(mac)
                    if src_state == "RUNNING":
                        state["last_active"] = now
                        status_updates.append((mac, True, 0))
                        log.debug(f"{mac}: audio streaming")
                    else:
                        idle_since = state.get("last_active", state["connected_at"])
                        idle_secs = int(now - idle_since)
                        remaining = IDLE_TIMEOUT_SECS - idle_secs
                        if idle_secs >= IDLE_TIMEOUT_SECS:
                            to_disconnect.append(mac)
                        else:
                            status_updates.append((mac, False, idle_secs))
                            log.info(
                                f"{mac}: no audio for {idle_secs}s "
                                f"(src_state={src_state}, "
                                f"disconnect in {remaining}s)"
                            )

            for mac, streaming, idle_secs in status_updates:
                push_bt_connected(mac, streaming=streaming, idle_secs=idle_secs)

            for mac in to_disconnect:
                log.info(f"{mac}: 30 min with no audio — disconnecting")
                bt_cmd("disconnect", mac)

        except Exception:
            log.exception("idle_monitor error (will retry in 30s)")


# ---------------------------------------------------------------------------
# BlueZ helpers
# ---------------------------------------------------------------------------

def get_device_name(mac):
    """Look up the human-readable name/alias of a BT device via DBus."""
    try:
        mac_path = mac.replace(":", "_")
        obj = _bus.get_object("org.bluez", f"/org/bluez/hci0/dev_{mac_path}")
        props_iface = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        name = props_iface.Get("org.bluez.Device1", "Alias")
        return str(name) if name else mac
    except Exception:
        return mac


def get_connected_devices():
    """Return (mac, name) pairs for devices currently connected (used at startup)."""
    try:
        obj = _bus.get_object("org.bluez", "/")
        om = dbus.Interface(obj, "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
        result = []
        for path, ifaces in objects.items():
            if "org.bluez.Device1" in ifaces:
                props = ifaces["org.bluez.Device1"]
                if props.get("Connected", False):
                    mac = str(path).split("/dev_")[-1].replace("_", ":")
                    name = str(props.get("Alias", props.get("Name", mac)))
                    result.append((mac, name))
        return result
    except Exception as e:
        log.warning(f"get_connected_devices: {e}")
        return []


# ---------------------------------------------------------------------------
# Radio BT status push helpers
# ---------------------------------------------------------------------------

def push_bt_connected(mac, streaming=False, idle_secs=0):
    with state_lock:
        name = device_state.get(mac, {}).get("name", mac)
    radio_post("bluetooth", {
        "connected": True,
        "device_name": name,
        "device_mac": mac,
        "streaming": streaming,
        "idle_secs": idle_secs,
        "idle_timeout": IDLE_TIMEOUT_SECS,
    })


def push_bt_disconnected():
    radio_post("bluetooth", {"connected": False})


# ---------------------------------------------------------------------------
# Connection / disconnection handlers
# ---------------------------------------------------------------------------

def on_device_connected(mac):
    name = get_device_name(mac)
    log.info(f"Device connected: {mac} ({name})")
    bt_cmd("trust", mac)  # ensure seamless reconnects without re-pairing

    status = radio_get("status")
    was_playing = bool(status.get("playing", False)) if status else False
    station_id = None
    if was_playing:
        station = status.get("current_station")
        station_id = station["id"] if station else None
        radio_post("pause")
        log.info(f"Radio paused (was playing station {station_id})")

    now = time.time()
    with state_lock:
        device_state[mac] = {
            "name": name,
            "was_playing": was_playing,
            "station_id": station_id,
            "connected_at": now,
            "last_active": now,
        }

    push_bt_connected(mac)
    GLib.timeout_add(3000, lambda: _try_create_loopback(mac))


def on_device_disconnected(mac):
    log.info(f"Device disconnected: {mac}")

    with state_lock:
        if mac in active_loopbacks:
            unload_loopback(active_loopbacks.pop(mac))
            log.info(f"Loopback removed for {mac}")
        state = device_state.pop(mac, {})

    push_bt_disconnected()

    if state.get("was_playing") and state.get("station_id") is not None:
        log.info(f"Resuming radio (station {state['station_id']})")
        radio_post("play", {"station_id": state["station_id"]})


def _try_create_loopback(mac):
    """GLib.timeout_add callback — returns False to fire only once."""
    with state_lock:
        if mac not in device_state or mac in active_loopbacks:
            return False

    source = find_bt_source(mac)
    if not source:
        log.warning(f"No A2DP source found for {mac}")
        return False

    idx = load_loopback(source)
    if idx is not None:
        with state_lock:
            active_loopbacks[mac] = idx
        log.info(f"Loopback: {source} → {ALSA_SINK} (module {idx})")
    else:
        log.warning(f"Failed to create loopback for {source}")
    return False


def on_properties_changed(interface, changed, invalidated, path, **kwargs):
    if interface != "org.bluez.Device1" or "Connected" not in changed:
        return
    if "/dev_" not in path:
        return
    mac = str(path).split("/dev_")[-1].replace("_", ":")
    if bool(changed["Connected"]):
        on_device_connected(mac)
    else:
        on_device_disconnected(mac)


# ---------------------------------------------------------------------------
# Pairing agent
# ---------------------------------------------------------------------------

class Agent(dbus.service.Object):
    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log.info(f"AuthorizeService: {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log.info(f"DisplayPasskey: {passkey}")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log.info(f"DisplayPinCode: {pincode}")

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log.info(f"RequestConfirmation: auto-confirming for {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log.info(f"RequestAuthorization: auto-authorizing {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _bus
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    _bus = dbus.SystemBus()

    setup_adapter()

    # Register pairing agent
    obj = _bus.get_object("org.bluez", "/org/bluez")
    manager = dbus.Interface(obj, "org.bluez.AgentManager1")
    agent = Agent(_bus, AGENT_PATH)
    manager.RegisterAgent(AGENT_PATH, AGENT_CAPABILITY)
    manager.RequestDefaultAgent(AGENT_PATH)
    log.info(f"Pairing agent registered ({AGENT_CAPABILITY})")

    # Populate device_state for any devices already connected at startup
    # (handles restarts while a phone is connected)
    for mac, name in get_connected_devices():
        log.info(f"Startup: already-connected device {mac} ({name})")
        now = time.time()
        with state_lock:
            device_state[mac] = {
                "name": name,
                "was_playing": False,
                "station_id": None,
                "connected_at": now,
                "last_active": now,
            }
        push_bt_connected(mac)
        GLib.timeout_add(1000, lambda m=mac: _try_create_loopback(m))

    # Watch for connect/disconnect events
    _bus.add_signal_receiver(
        on_properties_changed,
        signal_name="PropertiesChanged",
        dbus_interface="org.freedesktop.DBus.Properties",
        path_keyword="path",
    )

    # Start idle monitor
    threading.Thread(target=idle_monitor, daemon=True).start()

    log.info("Ready — waiting for connections")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        manager.UnregisterAgent(AGENT_PATH)
        sys.exit(0)


if __name__ == "__main__":
    main()
