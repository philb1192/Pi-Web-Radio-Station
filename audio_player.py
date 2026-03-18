"""
Audio Player Module
Handles audio playback on the Raspberry Pi using mpv
"""

import json
import logging
import os
import socket as _socket
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

class AudioPlayer:
    """Handles audio playback on the Raspberry Pi"""
    IPC_SOCKET = '/tmp/mpv-radio.sock'

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.current_url: Optional[str] = None
        self.current_volume: float = 0.9
        self._stopped_spotify: bool = False  # True if we stopped raspotify to play radio
        
    def _spotify_active(self) -> bool:
        """Return True if librespot currently holds the ALSA playback device."""
        try:
            r = subprocess.run(
                ['fuser', '/dev/snd/pcmC0D0p'],
                capture_output=True, text=True
            )
            for pid in r.stdout.split():
                comm_path = f'/proc/{pid.strip()}/comm'
                if os.path.exists(comm_path):
                    with open(comm_path) as f:
                        if f.read().strip() == 'librespot':
                            return True
        except Exception:
            pass
        return False

    def _pause_spotify(self):
        """Stop raspotify so librespot releases the ALSA device."""
        try:
            subprocess.run(['systemctl', 'stop', 'raspotify'], capture_output=True)
            time.sleep(0.5)
            logger.info("Stopped raspotify to free ALSA device for radio")
        except Exception as e:
            logger.warning(f"Could not stop raspotify: {e}")

    def _resume_spotify(self):
        """Restart raspotify after radio stops."""
        try:
            subprocess.run(['systemctl', 'start', 'raspotify'], capture_output=True)
            logger.info("Restarted raspotify")
        except Exception as e:
            logger.warning(f"Could not start raspotify: {e}")

    def _mpv_send(self, cmd: list) -> bool:
        """Send a JSON command to mpv via IPC socket. Returns True on success."""
        s = None
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(self.IPC_SOCKET)
            s.sendall((json.dumps({"command": cmd}) + '\n').encode())
            return True
        except Exception:
            return False
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass

    def _mpv_set_vol(self, percent: int):
        """Set mpv's internal software volume (0–100) via IPC."""
        self._mpv_send(["set_property", "volume", max(0, min(100, percent))])

    def fade_out(self, duration: float = 0.4, steps: int = 15):
        """Smoothly ramp mpv volume to 0. Blocking."""
        if not self.is_playing():
            return
        start = int(self.current_volume * 100)
        for i in range(steps, -1, -1):
            self._mpv_set_vol(int(start * i / steps))
            time.sleep(duration / steps)

    def fade_in(self, duration: float = 0.4, steps: int = 15):
        """Smoothly ramp mpv volume from 0 to current_volume. Blocking."""
        if not self.is_playing():
            return
        target = int(self.current_volume * 100)
        # Wait for IPC socket to be ready (mpv may still be starting)
        for _ in range(30):
            if self._mpv_send(["set_property", "volume", 0]):
                break
            time.sleep(0.05)
        for i in range(1, steps + 1):
            self._mpv_set_vol(int(target * i / steps))
            time.sleep(duration / steps)

    def stop_fade(self, release_spotify: bool = False, duration: float = 0.4):
        """Fade out then stop. Blocking."""
        self.fade_out(duration)
        self.stop(release_spotify)

    def play(self, url: str, start_silent: bool = False):
        """Start playing a stream. Pass start_silent=True when fade-in follows."""
        self.stop()  # stop mpv only, does not touch raspotify
        if self._spotify_active():
            self._pause_spotify()
            self._stopped_spotify = True
        try:
            # Set up environment for audio
            env = os.environ.copy()
            
            # Get the user's XDG_RUNTIME_DIR for PulseAudio socket
            user = os.getenv('USER', 'pi')
            uid = os.getuid()
            env['XDG_RUNTIME_DIR'] = f'/run/user/{uid}'
            
            # Try to find PulseAudio socket
            pulse_server = f'unix:/run/user/{uid}/pulse/native'
            if os.path.exists(f'/run/user/{uid}/pulse/native'):
                env['PULSE_SERVER'] = pulse_server
            
            # Using mpv for reliable streaming
            volume_percent = 0 if start_silent else int(self.current_volume * 100)
            self.process = subprocess.Popen(
                [
                    'mpv',
                    '--no-video',
                    f'--volume={volume_percent}',
                    '--audio-device=alsa/hw:0,0',  # Force headphone jack
                    '--really-quiet',
                    f'--input-ipc-server={AudioPlayer.IPC_SOCKET}',
                    url
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env
            )
            self.current_url = url
            logger.info(f"Started playback: {url} at volume {volume_percent}%")
            
            # Check if process started successfully
            time.sleep(0.5)
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                logger.error(f"MPV failed to start: {stderr.decode()}")
                return False
            
            return True
        except FileNotFoundError:
            logger.error("MPV not found! Install with: sudo apt install mpv")
            return False
        except Exception as e:
            logger.error(f"Failed to start playback: {e}")
            return False
    
    def stop(self, release_spotify: bool = False):
        """Stop playback. Pass release_spotify=True when the user explicitly
        pauses so raspotify is restarted and Spotify Connect becomes available again."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            finally:
                self.process = None
                self.current_url = None
            logger.info("Stopped playback")
        if release_spotify and self._stopped_spotify:
            self._stopped_spotify = False
            self._resume_spotify()
    
    def is_playing(self) -> bool:
        """Check if audio is currently playing"""
        if self.process:
            return self.process.poll() is None
        return False
    
    def set_volume(self, volume: float):
        """Set system volume (0.0 to 1.0)"""
        self.current_volume = volume
        try:
            percent = int(volume * 100)
            result = subprocess.run(
                ['amixer', 'sset', 'PCM', f'{percent}%'],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                # Try setting Master instead
                subprocess.run(
                    ['amixer', 'sset', 'Master', f'{percent}%'],
                    capture_output=True,
                    text=True
                )
            
            logger.info(f"Volume set to {percent}%")
            
            # If currently playing, restart with new volume
            if self.is_playing() and self.current_url:
                logger.info("Restarting playback with new volume")
                url = self.current_url
                self.stop()
                time.sleep(0.2)
                self.play(url)
                
        except Exception as e:
            logger.error(f"Failed to set volume: {e}")