# Improvements & Addons

## Current stack (already implemented)
- Internet radio streaming via mpv · Web UI (aiohttp + WebSocket)
- Bluetooth A2DP audio from phone · Bluetooth auto-pair & idle timeout
- Spotify Connect via raspotify (CooperStation) · ALSA conflict prevention
- Stream metadata (ICY tags) · Stream reconnection with station fallback
- TTS via Piper (async, queued) · Volume debounce · Station reordering
- Nginx on port 80 · mDNS at radio.local · Systemd services for everything

---

## High Priority

### 1. Auto-play default station on startup
The server sets `current_station` from config but never calls `play()`. The speaker
is silent after every reboot until someone manually presses Play.

Fix in `server.py __init__`, after setting `self.state['current_station']`:
```python
if default_station:
    self.audio_player.play(default_station['url'])
    self.state['playing'] = True
```

### 2. Sleep timer
Stop playback after N minutes. Useful for falling asleep to the radio.

- UI: button group in the player tab (30 / 60 / 90 min) with a countdown display
- `POST /api/sleep {"minutes": 30}` — cancels any existing timer, schedules pause
- Cancels automatically if the user manually pauses

### 3. Basic authentication
Anyone on the local network can control the radio and queue TTS messages.
Add HTTP Basic Auth via aiohttp middleware. Store a single username + bcrypt-hashed
password in `config.json`. The Nginx proxy can also enforce it at the edge.

### 4. Station health check
Periodically (every few minutes) do a quick HEAD/connect check on all station URLs
and mark each one as online/offline in the state. The UI shows a coloured dot next
to each station name. Saves the user from clicking a dead station.

---

## Features

### 5. Wake-up alarm
Schedule playback to start at a specific time. Store alarm in `config.json`
(time + station ID + enabled flag). A background async task checks every minute.
Show the alarm time in the UI with an on/off toggle.

### 6. Station search / filter
A text input above the station list that filters by name client-side with
JavaScript. Zero server changes needed.

### 7. Play history
Keep the last 20 stations played with timestamps in memory.
- `GET /api/history` returns the list
- "Recently Played" collapsible section in the UI with one-click replay

### 8. Station categories / groups
Add an optional `category` field to each station (News, Music, Talk, Sports…).
The stations tab shows collapsible groups instead of a flat list.
Stored in `stations.json`, editable from the Add Station form.

### 9. Multiple TTS voices
Piper supports multiple voice models (`.onnx` files in `~/piper/models/`).
- `GET /api/tts/voices` — list available models
- `POST /api/tts/voice` — switch active model
- Dropdown in the TTS tab

### 10. Spotify Connect status in the UI
Raspotify/librespot exposes playback state via its event hook (`spotify_event.sh`
already exists). Extend it to POST to `/api/spotify` with the current event
(`playing`, `paused`, `stopped`, `track_changed`). Display a Spotify status bar
in the UI similar to the Bluetooth bar — shows track name if available.

### 11. Radio station directory search
Integrate the [Radio Browser API](https://api.radio-browser.info/) (free, no key
needed). Add a "Search online" button in the Stations tab that queries by name or
country and lets the user add results directly to their list.

### 12. Weather / time TTS announcements
A background task that speaks the current time every hour (or on demand) and
optionally fetches a weather summary from `wttr.in` using plain HTTP and reads it
aloud via the existing TTS queue. No extra dependencies — just `urllib`.

### 13. Progressive Web App (PWA)
Add a `manifest.json` and minimal service worker so the web UI can be pinned to a
phone's home screen and used offline (showing last known state). The install prompt
appears automatically in Chrome/Safari after two visits.

### 14. Dark / light theme toggle
Add a theme button in the header. Save preference to `localStorage`. The CSS
already uses variables — just add a `[data-theme="dark"]` block with inverted
colours.

---

## Hardware Addons

### 15. Physical GPIO buttons
Wire momentary buttons to GPIO pins for play/pause, next station, volume up/down.
Run as a separate `gpio-control.service` using `gpiozero`:
```python
from gpiozero import Button
import requests
Button(17).when_pressed = lambda: requests.post('http://localhost:8080/api/play')
```

### 16. Rotary encoder for volume
A KY-040 encoder gives a physical volume knob. Use `gpiozero.RotaryEncoder`
to detect direction and step volume via the API. Pairs naturally with item 15.

### 17. OLED status display
Connect a 128×64 SSD1306 OLED via I2C. A separate service polls `/api/status`
and shows station name, volume, Bluetooth / Spotify state, and scrolling metadata.
Use the `luma.oled` library.

### 18. RGB LED status indicator
A single RGB LED (common-cathode, three GPIO pins) shows state at a glance:
- Green pulsing = radio playing
- Blue solid = Spotify playing
- Yellow = Bluetooth connected, radio paused
- Red flash = stream error / reconnecting

---

## Infrastructure & Integration

### 19. Scheduled config backups
A systemd timer running daily that copies `stations.json` and `config.json` to
`/home/pi/radio-backups/YYYY-MM-DD/`. Protects against SD card corruption.
```ini
# /etc/systemd/system/radio-backup.timer
[Timer]
OnCalendar=daily
Persistent=true
```

### 20. MPRIS D-Bus interface
Expose radio playback via the standard MPRIS2 D-Bus interface so the radio can be
controlled with `playerctl` from the terminal or from any MPRIS-aware tool
(e.g. waybar, i3status). Allows: `playerctl play-pause`, `playerctl next`, etc.

### 21. Home Assistant integration
Document (or script) the REST API as a Home Assistant `media_player` or
`rest_command` entity. Example automations:
- "Pause radio when doorbell rings"
- "Speak a TTS message when someone arrives home"
- "Start radio at 7 AM on weekdays"
The API already supports everything needed — it just needs YAML examples.

### 22. HTTPS with self-signed certificate
Configure Nginx to serve the UI over HTTPS. Prevents credentials (once basic auth
is added) from being sent in cleartext. Generate a LAN cert with:
```bash
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 3650 -nodes
```
