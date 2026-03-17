#!/usr/bin/env python3
"""
Internet Radio Server with Text-to-Speech
Audio plays on the Raspberry Pi, not in browser
"""

import asyncio
import json
import logging
import re
import shutil
import subprocess
from aiohttp import web
from typing import Optional
import os
import urllib.parse

from audio_player import AudioPlayer
from tts_engine import TTSEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class RadioServer:
    def __init__(self):
        self.stations_file = os.path.join(BASE_DIR, 'stations.json')
        self.config_file = os.path.join(BASE_DIR, 'config.json')
        self.playback_state_file = os.path.join(BASE_DIR, 'playback_state.json')
        
        # Load configuration
        config = self.load_config()
        
        # Load stations from file
        self.state = {
            'playing': False,
            'current_station': None,
            'volume': float(config.get('default_volume', 0.85)),
            'muted': False,
            'metadata': None,
            'bt_status': {'connected': False},
            'spotify_status': {'active': False, 'playing': False, 'track': '', 'artist': '', 'album': ''},
            'reconnect_status': None,
            'station_health': {},
            'preview_station': None,
            'audio_output': config.get('default_audio_output', 'pi'),
            'tts_downloads': {},
            'stations': self.load_stations()
        }
        
        # Set default station if configured
        default_station_id = config.get('default_station_id')
        if default_station_id:
            default_station = next((s for s in self.state['stations'] if s['id'] == default_station_id), None)
            if default_station:
                self.state['current_station'] = default_station
                logger.info(f"Default station set to: {default_station['name']}")
        
        self.audio_player = AudioPlayer()
        self.tts_engine = TTSEngine()
        if config.get('default_tts_model'):
            self.tts_engine.set_model(config['default_tts_model'])
        self.websockets: set = set()
        self.tts_queue: asyncio.Queue = asyncio.Queue()
        self._tts_interrupted_station = None  # station to resume after queue drains
        self._preview_was_playing = False
        self._preview_interrupted_station = None  # station to resume after preview stops
        self._voices_cache = None
        self._voices_cache_time = 0.0

        # Set initial volume
        self.audio_player.set_volume(self.state['volume'])

        # Resume playback if it was running before the last reboot
        saved = self.load_playback_state()
        if saved.get('playing') and saved.get('station_id') is not None:
            station = next(
                (s for s in self.state['stations'] if s['id'] == saved['station_id']),
                None
            )
            if station:
                # health data isn't available yet at startup — play directly;
                # the watchdog will handle it if the stream is actually down
                logger.info(f"Resuming playback after reboot: {station['name']}")
                self.state['current_station'] = station
                self.state['playing'] = True
                self.audio_player.play(station['url'])
            else:
                logger.warning(f"Saved station id={saved['station_id']} no longer exists — not resuming")
    
    def load_stations(self):
        """Load stations from JSON file"""
        try:
            if os.path.exists(self.stations_file):
                with open(self.stations_file, 'r') as f:
                    stations_data = json.load(f)
                    if stations_data and isinstance(stations_data, list):
                        if 'id' in stations_data[0]:
                            logger.info(f"Loaded {len(stations_data)} stations from {self.stations_file}")
                            return stations_data
                        else:
                            stations = []
                            for idx, station in enumerate(stations_data, 1):
                                stations.append({
                                    'id': idx,
                                    'name': station.get('name', 'Unknown'),
                                    'url': station.get('url', '')
                                })
                            logger.info(f"Loaded and converted {len(stations)} stations")
                            return stations
        except Exception as e:
            logger.error(f"Failed to load stations from file: {e}")
        
        logger.info("Using default stations")
        return [{'id': 1, 'name': 'Stats Radio', 'url': 'https://stream2.statsradio.com:8068/stream'}]
    
    def load_config(self):
        """Load configuration from JSON file"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    logger.info(f"Loaded configuration from {self.config_file}")
                    return config
        except Exception as e:
            logger.error(f"Failed to load config from file: {e}")
        return {}
    
    def save_config(self, config):
        """Save configuration to JSON file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
            logger.info(f"Saved configuration to {self.config_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to save config to file: {e}")
            return False
    
    def save_playback_state(self):
        """Persist playing/station so we can resume after an unexpected reboot."""
        try:
            station = self.state['current_station']
            data = {
                'playing': self.state['playing'],
                'station_id': station['id'] if station else None,
            }
            with open(self.playback_state_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"Could not save playback state: {e}")

    def load_playback_state(self):
        """Return persisted playback state, or defaults if none exists."""
        try:
            if os.path.exists(self.playback_state_file):
                with open(self.playback_state_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load playback state: {e}")
        return {'playing': False, 'station_id': None}

    def save_stations(self):
        """Save stations to JSON file"""
        try:
            with open(self.stations_file, 'w') as f:
                json.dump(self.state['stations'], f, indent=2)
            logger.info(f"Saved {len(self.state['stations'])} stations")
            return True
        except Exception as e:
            logger.error(f"Failed to save stations to file: {e}")
            return False
    
    async def _spot_check_station(self, station: dict):
        """Check a single station and broadcast the result."""
        result = await self._check_url_reachable(station['url'])
        self.state['station_health'][station['id']] = result
        await self.broadcast_state()

    async def _check_url_reachable(self, url: str, timeout: float = 5.0) -> bool:
        """Open a TCP connection to the station host/port. Returns True if reachable."""
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            if not host:
                return False
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _check_all_stations(self):
        """Check every station concurrently and update station_health."""
        stations = self.state['stations']
        if not stations:
            return
        results = await asyncio.gather(
            *[self._check_url_reachable(s['url']) for s in stations],
            return_exceptions=True
        )
        for station, result in zip(stations, results):
            self.state['station_health'][station['id']] = (
                result if isinstance(result, bool) else False
            )
        await self.broadcast_state()

    async def _station_health_loop(self):
        """Initial check 10 s after boot, then recheck every 5 minutes."""
        await asyncio.sleep(10)
        while True:
            try:
                logger.info("Running station health checks…")
                await self._check_all_stations()
                logger.info(
                    "Health check done: "
                    + ", ".join(
                        f"{s['name']}={'OK' if self.state['station_health'].get(s['id']) else 'FAIL'}"
                        for s in self.state['stations']
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("station_health_loop error")
            await asyncio.sleep(5 * 60)

    async def _metadata_watcher_loop(self):
        """Persistent background task that reads ICY stream metadata from mpv via IPC socket.
        Reconnects automatically whenever mpv restarts (station change, TTS, etc.)."""
        socket_path = AudioPlayer.IPC_SOCKET
        while True:
            # Only attempt connection while radio is playing
            if not self.state['playing'] or not os.path.exists(socket_path):
                await asyncio.sleep(1)
                continue

            reader = writer = None
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                # Ask mpv to push an event every time media-title changes
                cmd = json.dumps({"command": ["observe_property", 1, "media-title"]}) + "\n"
                writer.write(cmd.encode())
                await writer.drain()

                while True:
                    line = await reader.readline()
                    if not line:
                        break  # mpv closed the socket (stopped/restarted)
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get('event') == 'property-change' and event.get('name') == 'media-title':
                        title = event.get('data')  # None or a string like "Artist - Title"
                        if title != self.state['metadata']:
                            self.state['metadata'] = title
                            await self.broadcast_state()
                            logger.info(f"Metadata: {title}")

            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if writer:
                    try:
                        writer.close()
                    except Exception:
                        pass

            # Clear metadata when mpv disconnects, then retry after a short pause
            if self.state['metadata'] is not None:
                self.state['metadata'] = None
                await self.broadcast_state()
            await asyncio.sleep(1)

    async def _stream_watchdog_loop(self):
        """Poll every 5 s. When a stream drops unexpectedly:
        - retry the same station up to MAX_RETRIES times
        - then try remaining stations, skipping any known-offline ones first
        - if nothing works, stop playback
        """
        MAX_RETRIES_PER_STATION = 3
        POLL_INTERVAL = 5  # seconds

        same_station_retries = 0
        tried_ids: set = set()   # station IDs already attempted during this reconnect cycle
        reconnecting = False

        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)

                if not self.state['playing']:
                    if reconnecting:
                        reconnecting = False
                        same_station_retries = 0
                        tried_ids = set()
                    continue

                # Don't monitor stream health when audio is routed to the browser
                if self.state.get('audio_output', 'pi') == 'browser':
                    if reconnecting:
                        reconnecting = False
                        same_station_retries = 0
                        tried_ids = set()
                        self.state['reconnect_status'] = None
                    continue

                if self.audio_player.is_playing():
                    # Stream is healthy — clear any reconnect banner
                    if reconnecting:
                        reconnecting = False
                        same_station_retries = 0
                        tried_ids = set()
                        self.state['reconnect_status'] = None
                        await self.broadcast_state()
                    continue

                # ── stream dropped ──────────────────────────────────────────
                stations = self.state['stations']
                current = self.state['current_station']

                if not stations or not current:
                    self.state['playing'] = False
                    await self.broadcast_state()
                    continue

                if not reconnecting:
                    reconnecting = True
                    same_station_retries = 0
                    tried_ids = {current['id']}
                    logger.warning(f"Stream dropped: {current['name']}")

                if same_station_retries < MAX_RETRIES_PER_STATION:
                    same_station_retries += 1
                    msg = (f"Reconnecting to {current['name']} "
                           f"({same_station_retries}/{MAX_RETRIES_PER_STATION})…")
                    logger.info(msg)
                    self.state['reconnect_status'] = msg
                    await self.broadcast_state()
                    await self._play_current(current['url'])

                else:
                    # Move to next station — prefer stations not known-offline
                    health = self.state['station_health']
                    untried = [s for s in stations if s['id'] not in tried_ids]

                    # Skip known-offline stations if any online/unknown ones remain
                    candidates = [s for s in untried if health.get(s['id']) is not False]
                    if not candidates:
                        candidates = untried  # all offline — try them anyway (health may be stale)

                    if not candidates:
                        logger.warning("All stations failed — stopping playback")
                        self.audio_player.stop(release_spotify=True)
                        self.state['playing'] = False
                        self.state['reconnect_status'] = None
                        self.save_playback_state()
                        reconnecting = False
                        same_station_retries = 0
                        tried_ids = set()
                        await self.broadcast_state()
                        continue

                    next_station = candidates[0]
                    tried_ids.add(next_station['id'])
                    same_station_retries = 0
                    msg = f"Trying {next_station['name']}…"
                    logger.info(msg)
                    self.state['current_station'] = next_station
                    self.state['reconnect_status'] = msg
                    await self.broadcast_state()
                    await self._play_current(next_station['url'])

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stream_watchdog error")

    async def _tts_worker(self):
        """Background task that processes TTS requests one at a time from the queue."""
        while True:
            text, volume = await self.tts_queue.get()
            try:
                # Stop radio on the first item; remember which station to resume
                if self.state['playing']:
                    self._tts_interrupted_station = self.state['current_station']
                    self.audio_player.stop()
                    self.state['playing'] = False
                    await self.broadcast_state()

                self.tts_engine.set_volume(volume)
                await asyncio.to_thread(self.tts_engine.speak, text)
            except Exception:
                logger.exception("TTS worker error on item")
            finally:
                self.tts_queue.task_done()

            # Resume radio only when the queue is fully drained
            if self.tts_queue.empty() and self._tts_interrupted_station:
                station = self._tts_interrupted_station
                self._tts_interrupted_station = None
                await self._play_current(station['url'])
                self.state['playing'] = True
                await self.broadcast_state()
            else:
                await self.broadcast_state()  # update queue size in UI

    def _clear_preview(self):
        """Clear preview state (called when real playback starts)."""
        self.state['preview_station'] = None
        self._preview_was_playing = False
        self._preview_interrupted_station = None

    async def _play_current(self, url: str):
        """Start mpv on the Pi with fade-out/in. No-op when routed to browser."""
        if self.state.get('audio_output', 'pi') != 'pi':
            return
        if self.audio_player.is_playing():
            await asyncio.to_thread(self.audio_player.fade_out)
        success = await asyncio.to_thread(self.audio_player.play, url, True)
        if success:
            await asyncio.to_thread(self.audio_player.fade_in)

    def _next_playable_station(self, station: dict) -> dict:
        """Return station if online/unknown. If it is known-offline, walk forward
        through the station list and return the first online-or-unknown station.
        Falls back to the original station if every station is offline."""
        health = self.state['station_health']
        if health.get(station['id']) is not False:
            return station  # healthy or not yet checked

        stations = self.state['stations']
        start_idx = next((i for i, s in enumerate(stations) if s['id'] == station['id']), 0)
        for offset in range(1, len(stations)):
            candidate = stations[(start_idx + offset) % len(stations)]
            if health.get(candidate['id']) is not False:
                logger.info(
                    f"'{station['name']}' is offline — skipping to '{candidate['name']}'"
                )
                return candidate

        logger.warning(f"All stations offline — trying '{station['name']}' anyway")
        return station

    async def broadcast_state(self):
        """Broadcast state to all connected websockets"""
        if not self.websockets:
            return
        
        message = json.dumps({
            'type': 'state_update',
            'state': {**self.state, 'tts_queue_size': self.tts_queue.qsize()}
        })
        
        dead_sockets = set()
        for ws in self.websockets:
            try:
                await ws.send_str(message)
            except Exception:
                dead_sockets.add(ws)
        
        self.websockets -= dead_sockets
    
    async def websocket_handler(self, request):
        """Handle websocket connections"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        self.websockets.add(ws)
        logger.info(f"WebSocket connected. Total: {len(self.websockets)}")
        
        await ws.send_str(json.dumps({'type': 'state_update', 'state': self.state}))
        
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self.handle_websocket_message(data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f'WebSocket error: {ws.exception()}')
        finally:
            self.websockets.discard(ws)
            logger.info(f"WebSocket disconnected. Total: {len(self.websockets)}")
        
        return ws
    
    async def handle_websocket_message(self, data):
        """Handle messages from websocket clients"""
        action = data.get('action')
        
        if action == 'play':
            station_id = data.get('station_id')
            station = next((s for s in self.state['stations'] if s['id'] == station_id), None)
            if station:
                station = self._next_playable_station(station)
                self._clear_preview()
                self.state['current_station'] = station
                self.state['reconnect_status'] = None
                await self._play_current(station['url'])
                self.state['playing'] = True
                self.save_playback_state()

        elif action == 'pause':
            self._clear_preview()
            await asyncio.to_thread(self.audio_player.stop_fade, True)
            self.state['playing'] = False
            self.state['reconnect_status'] = None
            self.save_playback_state()

        elif action == 'toggle_play':
            if self.state['playing']:
                self._clear_preview()
                await asyncio.to_thread(self.audio_player.stop_fade, True)
                self.state['playing'] = False
                self.state['reconnect_status'] = None
                self.save_playback_state()
            else:
                if self.state['current_station']:
                    self._clear_preview()
                    station = self._next_playable_station(self.state['current_station'])
                    self.state['current_station'] = station
                    await self._play_current(station['url'])
                    self.state['playing'] = True
                    self.save_playback_state()
                elif self.state['preview_station']:
                    # Stop preview if toggle_play pressed with no real station
                    self.audio_player.stop(release_spotify=True)
                    self._clear_preview()
        
        elif action == 'set_volume':
            volume = float(data.get('volume', 0.9))
            self.state['volume'] = volume
            if not self.state['muted']:
                self.audio_player.set_volume(volume)
        
        elif action == 'toggle_mute':
            self.state['muted'] = not self.state['muted']
            if self.state['muted']:
                self.audio_player.set_volume(0)
            else:
                self.audio_player.set_volume(self.state['volume'])
        
        elif action == 'add_station':
            new_station = {
                'id': max([s['id'] for s in self.state['stations']], default=0) + 1,
                'name': data.get('name'),
                'url': data.get('url')
            }
            self.state['stations'].append(new_station)
            self.save_stations()
            asyncio.create_task(self._spot_check_station(new_station))
        
        elif action == 'delete_station':
            station_id = data.get('station_id')
            if self.state['current_station'] and self.state['current_station']['id'] == station_id:
                self.audio_player.stop()
                self.state['current_station'] = None
                self.state['playing'] = False
                self.save_playback_state()
            self.state['stations'] = [s for s in self.state['stations'] if s['id'] != station_id]
            self.save_stations()
        
        elif action == 'set_default_station':
            station_id = data.get('station_id')
            config = self.load_config()
            if station_id:
                config['default_station_id'] = station_id
            else:
                config.pop('default_station_id', None)
            self.save_config(config)
        
        await self.broadcast_state()
    
    async def index(self, request):
        """Serve the main HTML page"""
        with open(os.path.join(BASE_DIR, 'index.html'), 'r') as f:
            html = f.read()
        return web.Response(text=html, content_type='text/html')

    async def serve_help(self, request):
        """Serve the user guide page"""
        with open(os.path.join(BASE_DIR, 'help.html'), 'r') as f:
            html = f.read()
        return web.Response(text=html, content_type='text/html')

    async def serve_css(self, request):
        """Serve CSS file"""
        with open(os.path.join(BASE_DIR, 'style.css'), 'r') as f:
            css = f.read()
        return web.Response(text=css, content_type='text/css')

    async def serve_js(self, request):
        """Serve JavaScript file"""
        with open(os.path.join(BASE_DIR, 'script.js'), 'r') as f:
            js = f.read()
        return web.Response(text=js, content_type='application/javascript')
    
    # API Endpoints
    async def api_status(self, request):
        """Get current status"""
        return web.json_response({
            'playing': self.state['playing'],
            'current_station': self.state['current_station'],
            'volume': self.state['volume'],
            'muted': self.state['muted'],
            'metadata': self.state['metadata'],
            'stations': self.state['stations']
        })
    
    async def api_speak(self, request):
        """Text-to-speech endpoint — enqueues the message and returns immediately."""
        try:
            data = await request.json()
            text = data.get('text', '')
            volume = data.get('volume', 50)

            if not text:
                return web.json_response({'status': 'error', 'message': 'No text provided'}, status=400)

            await self.tts_queue.put((text, volume))
            logger.info(f"TTS queued: '{text[:60]}' (queue size: {self.tts_queue.qsize()})")
            await self.broadcast_state()
            return web.json_response({'status': 'ok', 'queued': self.tts_queue.qsize()})
        except Exception as e:
            logger.error(f"TTS error: {e}", exc_info=True)
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)
    
    async def api_speak_browser(self, request):
        """Generate TTS audio with Piper and return WAV bytes for browser playback."""
        try:
            data = await request.json()
            text = (data.get('text') or '').strip()
            if not text:
                return web.json_response({'status': 'error', 'message': 'No text'}, status=400)
            wav_bytes = await asyncio.to_thread(self.tts_engine.synthesize, text)
            if wav_bytes is None:
                return web.json_response({'status': 'error', 'message': 'TTS synthesis failed'}, status=500)
            return web.Response(body=wav_bytes, content_type='audio/wav')
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def api_play(self, request):
        """Play a station"""
        try:
            data = await request.json()
            station_id = data.get('station_id')
            
            if station_id:
                station = next((s for s in self.state['stations'] if s['id'] == station_id), None)
                if station:
                    station = self._next_playable_station(station)
                    self.state['current_station'] = station
                    await self._play_current(station['url'])
                    self.state['playing'] = True
                    self.save_playback_state()
                    await self.broadcast_state()
                    return web.json_response({'status': 'ok', 'playing': station['name']})
                else:
                    return web.json_response({'status': 'error', 'message': 'Station not found'}, status=404)

            if self.state['current_station']:
                station = self._next_playable_station(self.state['current_station'])
                self.state['current_station'] = station
                await self._play_current(station['url'])
                self.state['playing'] = True
                self.save_playback_state()
                await self.broadcast_state()
                return web.json_response({'status': 'ok', 'playing': station['name']})
            
            return web.json_response({'status': 'error', 'message': 'No station selected'}, status=400)
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)
    
    async def api_pause(self, request):
        """Pause playback"""
        await asyncio.to_thread(self.audio_player.stop_fade, True)
        self.state['playing'] = False
        self.save_playback_state()
        await self.broadcast_state()
        return web.json_response({'status': 'ok', 'playing': False})
    
    async def api_volume(self, request):
        """Set volume"""
        try:
            data = await request.json()
            volume = float(data.get('volume', 0.9))
            volume = max(0.0, min(1.0, volume))
            
            self.state['volume'] = volume
            if not self.state['muted']:
                self.audio_player.set_volume(volume)
            
            await self.broadcast_state()
            return web.json_response({'status': 'ok', 'volume': volume})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)
    
    async def api_export_stations(self, request):
        """Export stations as JSON"""
        stations_export = [{'name': s['name'], 'url': s['url']} for s in self.state['stations']]
        return web.json_response(stations_export, headers={'Content-Disposition': 'attachment; filename="radio_stations.json"'})
    
    async def api_import_stations(self, request):
        """Import stations from JSON"""
        try:
            data = await request.json()
            if not isinstance(data, list):
                return web.json_response({'status': 'error', 'message': 'Expected array of stations'}, status=400)
            
            for station in data:
                if 'name' not in station or 'url' not in station:
                    return web.json_response({'status': 'error', 'message': 'Each station must have name and url'}, status=400)
            
            max_id = max([s['id'] for s in self.state['stations']], default=0)
            imported_count = 0
            for station in data:
                max_id += 1
                self.state['stations'].append({'id': max_id, 'name': station['name'], 'url': station['url']})
                imported_count += 1
            
            self.save_stations()
            asyncio.create_task(self._check_all_stations())
            await self.broadcast_state()
            return web.json_response({'status': 'ok', 'message': f'Imported {imported_count} stations', 'count': imported_count})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)
    
    async def api_reorder_stations(self, request):
        """Reorder stations to match the supplied list of IDs."""
        try:
            data = await request.json()
            ids = data.get('ids', [])
            station_map = {s['id']: s for s in self.state['stations']}
            if set(ids) != set(station_map.keys()) or len(ids) != len(station_map):
                return web.json_response({'status': 'error', 'message': 'Invalid station IDs'}, status=400)
            self.state['stations'] = [station_map[i] for i in ids]
            self.save_stations()
            await self.broadcast_state()
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def api_directory_search(self, request):
        """Proxy a search to the Radio Browser public API."""
        import aiohttp as _aiohttp
        query = request.rel_url.query.get('q', '').strip()
        if not query:
            return web.json_response([])
        try:
            params = {
                'name': query,
                'limit': '30',
                'hidebroken': 'true',
                'order': 'votes',
                'reverse': 'true',
            }
            url = 'https://de1.api.radio-browser.info/json/stations/search?' + urllib.parse.urlencode(params)
            headers = {'User-Agent': 'CooperStation-Radio/1.0'}
            timeout = _aiohttp.ClientTimeout(total=10)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json(content_type=None)

            results = []
            for s in data:
                name = (s.get('name') or '').strip()
                stream_url = (s.get('url_resolved') or s.get('url') or '').strip()
                if not name or not stream_url:
                    continue
                results.append({
                    'name': name,
                    'url': stream_url,
                    'country': s.get('country', ''),
                    'language': s.get('language', ''),
                    'codec': s.get('codec', ''),
                    'bitrate': s.get('bitrate', 0),
                    'tags': s.get('tags', ''),
                    'votes': s.get('votes', 0),
                })
            return web.json_response(results)
        except Exception as e:
            logger.error(f"Directory search error: {e}")
            return web.json_response({'error': str(e)}, status=502)

    async def api_spotify(self, request):
        """Receive Spotify Connect status from the event hook script."""
        try:
            self.state['spotify_status'] = await request.json()
            await self.broadcast_state()
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def api_audio_output(self, request):
        """Switch audio output between 'pi' (mpv) and 'browser' (web client handles audio)."""
        try:
            data = await request.json()
            output = data.get('output', 'pi')
            if output not in ('pi', 'browser'):
                return web.json_response({'status': 'error', 'message': 'output must be "pi" or "browser"'}, status=400)

            old_output = self.state.get('audio_output', 'pi')
            self.state['audio_output'] = output

            if output == 'browser' and old_output == 'pi':
                # Stop mpv; browser will pick up audio via state broadcast
                if self.state['playing']:
                    self.audio_player.stop(release_spotify=False)
                    # Keep state['playing'] = True — browser uses that to know it should play

            elif output == 'pi' and old_output == 'browser':
                # Start mpv if we were logically playing
                if self.state['playing'] and self.state['current_station']:
                    self.audio_player.play(self.state['current_station']['url'])

            await self.broadcast_state()
            return web.json_response({'status': 'ok', 'audio_output': output})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def api_preview(self, request):
        """Preview a stream URL without adding it to the station list."""
        try:
            data = await request.json()
            url = (data.get('url') or '').strip()
            name = (data.get('name') or 'Preview').strip()
            if not url:
                return web.json_response({'status': 'error', 'message': 'No URL'}, status=400)

            # Save what was playing so we can restore it when preview ends
            self._preview_was_playing = self.state['playing']
            self._preview_interrupted_station = (
                self.state['current_station'] if self._preview_was_playing else None
            )

            # Play the preview — stops whatever was running
            self.audio_player.play(url)
            self.state['playing'] = False  # watchdog ignores preview traffic
            self.state['preview_station'] = {'name': name, 'url': url}
            await self.broadcast_state()
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def api_preview_stop(self, request):
        """Stop the current preview and restore previous playback."""
        self.audio_player.stop(release_spotify=False)
        self.state['preview_station'] = None

        if self._preview_was_playing and self._preview_interrupted_station:
            station = self._preview_interrupted_station
            self.audio_player.play(station['url'])
            self.state['playing'] = True

        self._preview_was_playing = False
        self._preview_interrupted_station = None
        await self.broadcast_state()
        return web.json_response({'status': 'ok'})

    async def api_bluetooth(self, request):
        """Receive Bluetooth status updates from bt_agent."""
        try:
            self.state['bt_status'] = await request.json()
            await self.broadcast_state()
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def api_tts_available(self, request):
        """Return all Piper voices from HuggingFace, annotated with installed status."""
        import aiohttp as _aiohttp
        import time
        if self._voices_cache and time.time() - self._voices_cache_time < 3600:
            return web.json_response(self._voices_cache)
        try:
            url = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json'
            timeout = _aiohttp.ClientTimeout(total=15)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    voices = await resp.json(content_type=None)
            installed_names = {m['name'] for m in self.tts_engine.list_models()}
            result = []
            for key, info in voices.items():
                files = info.get('files', {})
                onnx_rel = next((p for p in files if p.endswith('.onnx') and not p.endswith('.json')), None)
                if not onnx_rel:
                    continue
                json_rel = onnx_rel + '.json' if (onnx_rel + '.json') in files else None
                size_bytes = sum(f.get('size_bytes', 0) for f in files.values())
                lang = info.get('language', {})
                result.append({
                    'key': key,
                    'name': info.get('name', key),
                    'language': lang.get('code', ''),
                    'language_name': f"{lang.get('name_english', '')} ({lang.get('country_english', '')})",
                    'quality': info.get('quality', ''),
                    'installed': os.path.basename(onnx_rel)[:-5] in installed_names,
                    'onnx_path': onnx_rel,
                    'json_path': json_rel,
                    'size_mb': round(size_bytes / 1024 / 1024, 1),
                })
            result.sort(key=lambda x: (x['language'], x['name'], x['quality']))
            self._voices_cache = result
            self._voices_cache_time = time.time()
            return web.json_response(result)
        except Exception as e:
            logger.error(f"Failed to fetch available voices: {e}")
            return web.json_response({'error': str(e)}, status=502)

    async def api_tts_download(self, request):
        """Start a background download of a Piper voice model."""
        try:
            data = await request.json()
            key = data.get('key', '').strip()
            onnx_path = data.get('onnx_path', '').strip()
            json_path = data.get('json_path', '').strip()
            if not key or not onnx_path:
                return web.json_response({'status': 'error', 'message': 'Missing key or path'}, status=400)
            if key in self.state['tts_downloads']:
                return web.json_response({'status': 'error', 'message': 'Already downloading'}, status=400)
            asyncio.create_task(self._download_model_task(key, onnx_path, json_path))
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def _download_model_task(self, key: str, onnx_rel: str, json_rel: str):
        """Background task: download .onnx (and .json) from HuggingFace."""
        import aiohttp as _aiohttp
        import time
        models_dir = os.path.expanduser('~/piper/models')
        os.makedirs(models_dir, exist_ok=True)
        base_url = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/'
        files = [(onnx_rel, os.path.join(models_dir, os.path.basename(onnx_rel)))]
        if json_rel:
            files.append((json_rel, os.path.join(models_dir, os.path.basename(json_rel))))
        self.state['tts_downloads'][key] = {'progress': 0.0}
        await self.broadcast_state()
        try:
            timeout = _aiohttp.ClientTimeout(total=600)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                for idx, (rel, dest) in enumerate(files):
                    async with session.get(base_url + rel) as resp:
                        resp.raise_for_status()
                        total = int(resp.headers.get('content-length', 0))
                        done = 0
                        last_broadcast = 0.0
                        with open(dest, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(65536):
                                f.write(chunk)
                                done += len(chunk)
                                now = time.time()
                                if total and now - last_broadcast >= 1.0:
                                    progress = (idx + done / total) / len(files)
                                    self.state['tts_downloads'][key] = {'progress': round(progress, 2)}
                                    await self.broadcast_state()
                                    last_broadcast = now
            del self.state['tts_downloads'][key]
            self._voices_cache = None  # invalidate so installed status refreshes
            logger.info(f"Downloaded voice model: {key}")
        except Exception as e:
            logger.error(f"Download failed for {key}: {e}")
            self.state['tts_downloads'][key] = {'progress': -1, 'error': str(e)}
        await self.broadcast_state()

    async def api_tts_delete(self, request):
        """Delete an installed Piper voice model."""
        try:
            data = await request.json()
            model_path = os.path.abspath(data.get('path', ''))
            models_dir = os.path.abspath(os.path.expanduser('~/piper/models'))
            if not model_path.startswith(models_dir + os.sep):
                return web.json_response({'status': 'error', 'message': 'Invalid path'}, status=400)
            if model_path == os.path.abspath(self.tts_engine.model_path or ''):
                return web.json_response(
                    {'status': 'error', 'message': 'Cannot delete the active model — select another first'}, status=400)
            for path in [model_path, model_path + '.json']:
                if os.path.exists(path):
                    os.unlink(path)
            self._voices_cache = None
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def api_sysinfo(self, request):
        """Return system stats: CPU temp, memory, disk, load, uptime."""
        info = {}

        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                info['cpu_temp'] = round(int(f.read().strip()) / 1000, 1)
        except Exception:
            info['cpu_temp'] = None

        try:
            with open('/proc/uptime') as f:
                info['uptime_seconds'] = int(float(f.read().split()[0]))
        except Exception:
            info['uptime_seconds'] = None

        try:
            mem = {}
            with open('/proc/meminfo') as f:
                for line in f:
                    parts = line.split()
                    if parts[0] in ('MemTotal:', 'MemAvailable:'):
                        mem[parts[0]] = int(parts[1]) * 1024
            info['mem_total'] = mem.get('MemTotal:')
            info['mem_available'] = mem.get('MemAvailable:')
            info['mem_used'] = info['mem_total'] - info['mem_available']
            info['mem_percent'] = round(info['mem_used'] / info['mem_total'] * 100, 1)
        except Exception:
            info['mem_total'] = info['mem_available'] = info['mem_used'] = info['mem_percent'] = None

        try:
            disk = shutil.disk_usage('/')
            info['disk_total'] = disk.total
            info['disk_used'] = disk.used
            info['disk_free'] = disk.free
            info['disk_percent'] = round(disk.used / disk.total * 100, 1)
        except Exception:
            info['disk_total'] = info['disk_used'] = info['disk_free'] = info['disk_percent'] = None

        try:
            info['cpu_load'] = round(os.getloadavg()[0], 2)
        except Exception:
            info['cpu_load'] = None

        return web.json_response(info)

    def _get_bluetooth_name(self) -> str:
        try:
            r = subprocess.run(['bluetoothctl', 'show'], capture_output=True, text=True, timeout=3)
            m = re.search(r'^\s*Alias:\s*(.+)$', r.stdout, re.MULTILINE)
            return m.group(1).strip() if m else ''
        except Exception:
            return ''

    def _set_bluetooth_name(self, name: str):
        subprocess.run(['bluetoothctl', 'system-alias', name], capture_output=True, timeout=5)

    def _get_spotify_name(self) -> str:
        try:
            r = subprocess.run(['sudo', 'cat', '/etc/raspotify/conf'], capture_output=True, text=True, timeout=3)
            m = re.search(r'^LIBRESPOT_NAME="?([^"\n]+)"?\s*$', r.stdout, re.MULTILINE)
            return m.group(1).strip() if m else ''
        except Exception:
            return ''

    def _set_spotify_name(self, name: str):
        r = subprocess.run(['sudo', 'cat', '/etc/raspotify/conf'], capture_output=True, text=True, timeout=3)
        new_content = re.sub(
            r'^LIBRESPOT_NAME=.*$',
            f'LIBRESPOT_NAME="{name}"',
            r.stdout, flags=re.MULTILINE
        )
        proc = subprocess.Popen(['sudo', 'tee', '/etc/raspotify/conf'],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        proc.communicate(new_content.encode())
        subprocess.run(['sudo', 'systemctl', 'restart', 'raspotify'], capture_output=True, timeout=10)

    async def api_get_config(self, request):
        """Get current configuration including device names"""
        config = self.load_config()
        config['bluetooth_name'] = self._get_bluetooth_name()
        config['spotify_name'] = self._get_spotify_name()
        return web.json_response(config)

    async def api_save_config(self, request):
        """Save user settings; applies relevant ones immediately."""
        try:
            data = await request.json()
            config = self.load_config()
            allowed = {'default_volume', 'default_tts_volume', 'default_audio_output', 'default_tts_model'}
            for key in allowed:
                if key in data:
                    config[key] = data[key]
            self.save_config(config)

            # Apply immediately
            if 'default_volume' in data:
                vol = max(0.0, min(1.0, float(data['default_volume'])))
                self.state['volume'] = vol
                if not self.state['muted'] and self.state.get('audio_output', 'pi') == 'pi':
                    self.audio_player.set_volume(vol)
            if 'default_tts_model' in data and data['default_tts_model']:
                self.tts_engine.set_model(data['default_tts_model'])
            if 'bluetooth_name' in data and data['bluetooth_name'].strip():
                await asyncio.get_event_loop().run_in_executor(
                    None, self._set_bluetooth_name, data['bluetooth_name'].strip())
            if 'spotify_name' in data and data['spotify_name'].strip():
                await asyncio.get_event_loop().run_in_executor(
                    None, self._set_spotify_name, data['spotify_name'].strip())

            await self.broadcast_state()
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    async def api_tts_models(self, request):
        """List available Piper voice models."""
        return web.json_response(self.tts_engine.list_models())
    
    def cleanup(self):
        """Cleanup on shutdown"""
        self.audio_player.stop()

def main():
    server = RadioServer()
    
    app = web.Application()
    app.router.add_get('/', server.index)
    app.router.add_get('/help', server.serve_help)
    app.router.add_get('/style.css', server.serve_css)
    app.router.add_get('/script.js', server.serve_js)
    app.router.add_get('/ws', server.websocket_handler)
    
    # REST API endpoints
    app.router.add_get('/api/status', server.api_status)
    app.router.add_post('/api/speak', server.api_speak)
    app.router.add_post('/api/play', server.api_play)
    app.router.add_post('/api/pause', server.api_pause)
    app.router.add_post('/api/volume', server.api_volume)
    app.router.add_get('/api/export', server.api_export_stations)
    app.router.add_post('/api/import', server.api_import_stations)
    app.router.add_post('/api/stations/reorder', server.api_reorder_stations)
    app.router.add_post('/api/bluetooth', server.api_bluetooth)
    app.router.add_post('/api/spotify', server.api_spotify)
    app.router.add_get('/api/sysinfo', server.api_sysinfo)
    app.router.add_get('/api/config', server.api_get_config)
    app.router.add_post('/api/config', server.api_save_config)
    app.router.add_get('/api/tts/models', server.api_tts_models)
    app.router.add_get('/api/tts/available', server.api_tts_available)
    app.router.add_post('/api/tts/download', server.api_tts_download)
    app.router.add_post('/api/tts/delete', server.api_tts_delete)
    app.router.add_get('/api/directory/search', server.api_directory_search)
    app.router.add_post('/api/speak-browser', server.api_speak_browser)
    app.router.add_post('/api/preview', server.api_preview)
    app.router.add_post('/api/preview/stop', server.api_preview_stop)
    app.router.add_post('/api/audio-output', server.api_audio_output)
    
    async def on_startup(app):
        app['metadata_task'] = asyncio.create_task(server._metadata_watcher_loop())
        app['tts_task'] = asyncio.create_task(server._tts_worker())
        app['watchdog_task'] = asyncio.create_task(server._stream_watchdog_loop())
        app['health_task'] = asyncio.create_task(server._station_health_loop())

    async def cleanup_on_shutdown(app):
        app['metadata_task'].cancel()
        app['tts_task'].cancel()
        app['watchdog_task'].cancel()
        app['health_task'].cancel()
        server.cleanup()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(cleanup_on_shutdown)
    
    logger.info("Starting Radio Server on http://0.0.0.0:8080")
    web.run_app(app, host='0.0.0.0', port=8080)

if __name__ == '__main__':
    main()