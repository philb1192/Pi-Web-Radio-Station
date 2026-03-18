# Improvements & Addons

## Current stack (implemented)

**Core**
- Internet radio streaming via mpv · Web UI (aiohttp + WebSocket)
- Stream reconnection with station fallback · Stream health checks
- Station reordering (drag-and-drop) · Online Radio Browser directory search
- Station add/delete · Default station · Import/export stations (JSON)
- Full backup & restore (stations + settings) · Volume debounce

**Audio**
- Fade in/out on play, pause, and station switch (mpv IPC)
- Bluetooth A2DP from phone · Bluetooth auto-pair & idle timeout
- Spotify Connect via raspotify · ALSA conflict prevention
- Browser audio output mode (stream plays in browser tab)
- Volume control works for both Pi and browser modes

**TTS**
- Piper TTS (async, queued) · Browser TTS mode (WAV streamed to client)
- Multiple voice models · Download/delete models from Settings
- TTS volume separate from radio volume

**UI**
- Dark / light theme toggle · Responsive single-page app
- Station preview in directory search (plays in browser, no Pi audio)
- Spotify Connect status bar · Bluetooth status bar
- Settings: volume, audio output, TTS model, Bluetooth/Spotify names
- System info panel (CPU temp, memory, disk, uptime) — auto-refresh
- User guide at `/help`

**Infrastructure**
- Systemd service with `Restart=always`, circuit-breaker limits, journal logging
- Nginx on port 80 · mDNS at `radio.local`
- Backup/restore endpoint (`/api/backup`, `/api/restore`)
- One-command installer (`install.sh`)

---

## High Priority

### 1. Auto-play default station on startup
The server sets `current_station` from config on boot but never calls `play()`.
The speaker is silent after every reboot until someone manually presses Play.

Fix in `server.py __init__`, after loading playback state:
```python
if self.state['playing'] and self.state['current_station']:
    asyncio.get_event_loop().call_soon(
        lambda: asyncio.create_task(self._play_current(self.state['current_station']['url']))
    )
```

### 2. Sleep timer
Stop playback after N minutes. Useful for falling asleep to the radio.

- UI: button group (15 / 30 / 60 / 90 min) with a countdown shown near the Play button
- `POST /api/sleep {"minutes": 30}` — cancels any existing timer, schedules pause
- `DELETE /api/sleep` — cancel
- Cancels automatically if the user manually pauses

### 3. Basic authentication
Anyone on the local network can control the radio and queue TTS messages.
Add HTTP Basic Auth via aiohttp middleware. Store a single username +
bcrypt-hashed password in `config.json`. Settings page to change password.

---

## Features

### 4. Wake-up alarm
Schedule playback to start at a specific time. Store alarm in `config.json`
(time + station ID + enabled flag). A background async task checks every minute.
Show the alarm time in the Settings tab with an on/off toggle.

### 5. Station search / filter
A text input above the station list that filters by name client-side.
Zero server changes needed — pure JavaScript.

### 6. Play history
Keep the last 20 stations played with timestamps in memory.
- `GET /api/history` returns the list
- "Recently Played" collapsible section in the Stations tab

### 7. Station categories / groups
Add an optional `category` field to each station (News, Music, Talk, Sports…).
The Stations tab shows collapsible groups instead of a flat list.
Stored in `stations.json`, editable from the Add Station form.

### 8. Weather / time TTS announcements
A background task that speaks the current time every hour (or on demand) and
optionally fetches a weather summary from `wttr.in` via plain HTTP.
No extra dependencies — just `urllib`.

### 9. Progressive Web App (PWA)
Add a `manifest.json` and minimal service worker so the UI installs as an app
on phones. The install prompt appears automatically in Chrome/Safari.

### 10. Keyboard shortcuts
- `Space` — play/pause
- `←` / `→` — previous/next station
- `↑` / `↓` — volume up/down
- `m` — mute/unmute

---

## Hardware Addons

### 11. Physical GPIO buttons
Wire momentary buttons to GPIO pins for play/pause, next station, vol up/down.
Run as a separate `gpio-control.service` using `gpiozero`:
```python
from gpiozero import Button
import requests
Button(17).when_pressed = lambda: requests.post('http://localhost:8080/api/play')
```

### 12. Rotary encoder for volume
A KY-040 encoder gives a physical volume knob. Use `gpiozero.RotaryEncoder`
to detect direction and step volume via the API. Pairs naturally with item 11.

### 13. OLED status display
Connect a 128×64 SSD1306 OLED via I2C. A separate service polls `/api/status`
and shows station name, volume, Bluetooth / Spotify state, and scrolling metadata.
Use the `luma.oled` library.

### 14. RGB LED status indicator
A single RGB LED (common-cathode, three GPIO pins) shows state at a glance:
- Green pulsing = radio playing
- Blue solid = Spotify playing
- Yellow = Bluetooth connected, radio paused
- Red flash = stream error / reconnecting

---

## Infrastructure & Integration

### 15. Scheduled automatic backups
A systemd timer running daily that copies `stations.json` and `config.json` to
`~/radio-backups/YYYY-MM-DD/`. Protects against SD card corruption.
```ini
[Timer]
OnCalendar=daily
Persistent=true
```

### 16. Home Assistant `media_player` entity
Document the REST API as a HA `media_player` or `rest_command` entity.
Example automations: pause on doorbell, TTS on arrival, start at 7 AM on weekdays.
The API already supports everything needed.

### 17. HTTPS with self-signed certificate
Configure Nginx to serve over HTTPS. Prevents credentials (once auth is added)
from being sent in cleartext. The WebSocket code already supports `wss://`.
```bash
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 3650 -nodes
```

### 18. MPRIS D-Bus interface
Expose playback via the standard MPRIS2 D-Bus interface so the radio can be
controlled with `playerctl` or any MPRIS-aware tool (waybar, i3status, etc.).

---

## Known Issues / Technical Debt

- No rate limiting on `/api/speak` — a client could flood the TTS queue.
