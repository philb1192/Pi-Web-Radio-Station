# CooperStation 🎵

A self-hosted internet radio player for the Raspberry Pi with a clean web interface, Text-to-Speech announcements, Bluetooth sink, and Spotify Connect integration.

Audio plays through the Pi's speakers. The web UI can be opened on any device on your local network — phone, tablet, or computer.

---

## Features

- **Internet Radio** — stream any HTTP/HTTPS radio station via mpv
- **Web UI** — responsive single-page app, works on desktop and mobile
- **Online Station Directory** — search 30 000+ stations via the free [Radio Browser API](https://www.radio-browser.info), with a preview button before adding
- **Audio Output Toggle** — switch between Pi speakers and browser audio in one click
- **Stream Watchdog** — auto-reconnects on drop; cycles through other stations if the current one stays down
- **Station Health Checks** — TCP reachability check every 5 minutes, coloured dots in the UI
- **Text-to-Speech** — [Piper TTS](https://github.com/rhasspy/piper) integration; radio pauses, speaks, then resumes
- **TTS Queue** — multiple messages queue up and play in order
- **Bluetooth A2DP Sink** — phone connects to CooperStation like a Bluetooth speaker
- **Spotify Connect** — appears in the Spotify app as a speaker on your local network
- **Dark / Light Theme** — toggle with one click, preference saved in the browser
- **Settings Page** — persistent defaults for volume, audio output, TTS voice, and more
- **Voice Model Manager** — browse, download, and delete Piper voices from the UI
- **Auto-resume on Reboot** — resumes the last-playing station after a power cycle
- **REST API** — every action is available over HTTP (perfect for Home Assistant / Node-RED)
- **WebSocket** — real-time state sync to all open browser tabs
- **User Guide** — built-in help page at `/help`

---

## Hardware Requirements

- Raspberry Pi (tested on Pi 4, should work on Pi 3B+)
- Speaker or headphones connected to the 3.5 mm jack, or HDMI audio
- Internet connection (Wi-Fi or Ethernet)
- Optional: Bluetooth adapter (built-in on Pi 3/4)

---

## Software Requirements

| Package | Purpose |
|---------|---------|
| `python3` (3.11+) | Server runtime |
| `mpv` | Stream playback |
| `aplay` / `alsa-utils` | Audio device control |
| `amixer` | Volume control |
| `bluetoothd` / `bluez` | Bluetooth support (optional) |
| `raspotify` | Spotify Connect (optional) |
| [Piper TTS](https://github.com/rhasspy/piper) | Text-to-Speech (optional) |

---

## Installation

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y mpv alsa-utils python3 python3-venv python3-pip
```

### 2. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/cooperstation.git
cd cooperstation
```

### 3. Create a Python virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install aiohttp
```

### 4. Copy the example config files

```bash
cp stations.example.json stations.json
cp config.example.json config.json
```

Edit `stations.json` to add your favourite stations, or use the web UI to manage them after starting.

### 5. (Optional) Install Piper TTS

Download the Piper binary and a voice model:

```bash
# Create directories
mkdir -p ~/piper/piper ~/piper/models

# Download Piper for ARM64 (Raspberry Pi 4)
# Visit https://github.com/rhasspy/piper/releases for the latest release
wget -O /tmp/piper.tar.gz \
  https://github.com/rhasspy/piper/releases/latest/download/piper_arm64.tar.gz
tar -xzf /tmp/piper.tar.gz -C ~/piper/piper --strip-components=1

# Download a voice model (example: Amy, low quality, English US)
wget -O ~/piper/models/en_US-amy-low.onnx \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en_US/amy/low/en_US-amy-low.onnx
wget -O ~/piper/models/en_US-amy-low.onnx.json \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en_US/amy/low/en_US-amy-low.onnx.json
```

Additional voice models can be downloaded from the **Settings → Voice Models** page inside the web UI.

### 6. (Optional) Set up Spotify Connect

```bash
curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
```

Edit `/etc/raspotify/conf` and set:
```
LIBRESPOT_NAME="CooperStation"
LIBRESPOT_DEVICE=hw:0,0
LIBRESPOT_ONEVENT=/home/pi/cooperstation/spotify_event.sh
```

Make the event script executable:
```bash
chmod +x spotify_event.sh
sudo systemctl enable --now raspotify
```

### 7. (Optional) Set up Bluetooth

See `bt_agent.py` — it handles auto-accept pairing and runs as a background service. Rename the device:
```bash
echo 'PRETTY_HOSTNAME=CooperStation' | sudo tee /etc/machine-info
```

---

## Running

```bash
source venv/bin/activate
python3 server.py
```

The server listens on `http://0.0.0.0:8080`. Open `http://<your-pi-ip>:8080` in any browser on your network.

### Run on boot with systemd

```bash
sudo nano /etc/systemd/system/cooperstation.service
```

```ini
[Unit]
Description=CooperStation Internet Radio
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/cooperstation
ExecStart=/home/pi/cooperstation/venv/bin/python3 server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now cooperstation
```

---

## Configuration

All settings can be managed from the **⚙️ Settings** tab in the UI and are saved to `config.json`. You can also edit it directly:

| Key | Default | Description |
|-----|---------|-------------|
| `default_station_id` | `null` | Station pre-selected on page load |
| `default_volume` | `0.85` | Volume on startup (0.0–1.0) |
| `default_audio_output` | `"pi"` | `"pi"` or `"browser"` |
| `default_tts_volume` | `50` | TTS volume (0–100) |
| `default_tts_model` | `""` | Full path to the `.onnx` voice model |

---

## REST API

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| `GET` | `/api/status` | — | Current server state |
| `POST` | `/api/play` | `{"station_id": 1}` | Play a station |
| `POST` | `/api/pause` | — | Stop playback |
| `POST` | `/api/volume` | `{"volume": 0.75}` | Set volume (0.0–1.0) |
| `POST` | `/api/speak` | `{"text": "Hi", "volume": 70}` | Queue TTS (Pi output) |
| `POST` | `/api/audio-output` | `{"output": "pi"}` | Switch output |
| `GET` | `/api/directory/search?q=jazz` | — | Search Radio Browser |
| `GET` | `/api/export` | — | Export stations as JSON |
| `POST` | `/api/import` | stations array | Bulk-import stations |

WebSocket at `/ws` — send JSON action objects, receive real-time state updates.

Full documentation is available in the built-in user guide at **`/help`**.

---

## Project Structure

```
cooperstation/
├── server.py            # Main aiohttp server — API, WebSocket, state management
├── audio_player.py      # mpv wrapper + ALSA volume + Spotify conflict prevention
├── tts_engine.py        # Piper TTS integration
├── bt_agent.py          # Bluetooth auto-accept pairing agent
├── spotify_event.sh     # Librespot event hook (Python script)
├── index.html           # Single-page web UI
├── script.js            # Frontend logic
├── style.css            # CSS with dark/light theme variables
├── help.html            # Built-in user guide
├── stations.example.json
└── config.example.json
```

---

## Security Note

The web server has **no authentication**. It is designed for use on a trusted home network. Do not expose port 8080 directly to the internet — use a VPN (e.g. WireGuard or Tailscale) for remote access.

---

## Contributing

Pull requests are welcome. For major changes, open an issue first to discuss what you'd like to change.

---

## License

[MIT](LICENSE)
