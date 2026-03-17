#!/usr/bin/env python3
"""
Librespot/raspotify event hook.
Called by librespot for every player event; all relevant env vars are set by librespot.
"""
import json
import os
import urllib.request

event = os.environ.get("PLAYER_EVENT", "")

# Pause the radio whenever Spotify starts using the audio device
if event in ("playing", "started", "sink_opened"):
    try:
        req = urllib.request.Request(
            "http://localhost:8080/api/pause",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass

# Build the payload for the Spotify status endpoint
if event in ("playing", "started", "changed"):
    payload = {
        "active": True,
        "playing": True,
        "track": os.environ.get("NAME", ""),
        "artist": os.environ.get("ARTISTS", ""),
        "album": os.environ.get("ALBUM", ""),
    }
elif event == "paused":
    payload = {
        "active": True,
        "playing": False,
        "track": os.environ.get("NAME", ""),
        "artist": os.environ.get("ARTISTS", ""),
        "album": os.environ.get("ALBUM", ""),
    }
elif event in ("stopped", "sink_closed"):
    payload = {"active": False, "playing": False, "track": "", "artist": "", "album": ""}
else:
    # Nothing to report for other events (volume_set, shuffle_changed, etc.)
    raise SystemExit(0)

try:
    req = urllib.request.Request(
        "http://localhost:8080/api/spotify",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=3)
except Exception:
    pass
