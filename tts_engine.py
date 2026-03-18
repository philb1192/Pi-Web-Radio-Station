"""
Text-to-Speech Engine Module
Handles text-to-speech using Piper TTS
"""

import logging
import subprocess
import os
import tempfile

logger = logging.getLogger(__name__)

class TTSEngine:
    """Handles text-to-speech on the Raspberry Pi using Piper TTS"""
    def __init__(self):
        # Piper configuration - Set explicit paths
        self.piper_path = None
        self.model_path = None
        self.output_raw = False
        self.volume = 50  # Default TTS volume (0-100 for consistency)
        
        # Try to find piper executable
        piper_locations = [
            'piper',  # Try command in PATH first
            os.path.expanduser('~/radio-server/venv/bin/piper'),  # Virtualenv
            '/usr/local/bin/piper',
            '/usr/bin/piper',
            os.path.expanduser('~/piper/piper/piper'),
        ]
        
        for path in piper_locations:
            try:
                if path == 'piper':
                    # Check if it's in PATH
                    result = subprocess.run(['which', 'piper'], capture_output=True, text=True)
                    if result.returncode == 0:
                        self.piper_path = 'piper'
                        logger.info(f"Found piper in PATH: {result.stdout.strip()}")
                        break
                elif os.path.exists(path):
                    self.piper_path = path
                    logger.info(f"Found piper at: {path}")
                    break
            except Exception:
                continue
        
        # Auto-detect voice model
        self.model_path = self._find_voice_model()
        
        if self.piper_path:
            logger.info(f"Piper TTS initialized with executable: {self.piper_path}")
            if self.model_path:
                logger.info(f"Using voice model: {self.model_path}")
            else:
                logger.warning("No voice model found. Set model_path manually or download a model.")
                logger.warning("Expected location: ~/piper/models/*.onnx")
        else:
            logger.error("Piper executable not found!")
            logger.error("Install with: pip install piper-tts")
            logger.error("Or download from: https://github.com/rhasspy/piper/releases")
    
    def _find_voice_model(self):
        """Auto-detect Piper voice model"""
        # Common voice model locations
        possible_locations = [
            # Home directory
            os.path.expanduser('~/piper/models'),
            os.path.expanduser('~/piper'),
            # Current directory
            './piper/models',
            './models',
            # System-wide
            '/usr/share/piper/models',
            '/usr/local/share/piper/models',
        ]
        
        for location in possible_locations:
            if os.path.isdir(location):
                # Look for .onnx files (Piper voice models)
                for root, dirs, files in os.walk(location):
                    for file in files:
                        if file.endswith('.onnx'):
                            return os.path.join(root, file)
        
        return None
    
    def speak(self, text: str):
        """Speak the given text using Piper TTS"""
        logger.info(f"TTS speak() called with text: '{text}' at volume: {self.volume}")
        logger.info(f"Piper path: {self.piper_path}")
        logger.info(f"Model path: {self.model_path}")
        
        if not self.piper_path:
            logger.error("Piper TTS executable not found")
            return False
        
        if not self.model_path:
            logger.error("No voice model configured")
            return False
        
        try:
            logger.info(f"Starting Piper TTS generation...")

            # Set up environment for audio
            env = os.environ.copy()
            uid = os.getuid()
            env['XDG_RUNTIME_DIR'] = f'/run/user/{uid}'

            # Create a temporary WAV file
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_wav:
                temp_wav_path = temp_wav.name

            try:
                # Run Piper to generate speech
                piper_process = subprocess.Popen(
                    [self.piper_path, '--model', self.model_path, '--output_file', temp_wav_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True
                )

                # Send text to piper
                stdout, stderr = piper_process.communicate(input=text, timeout=30)

                if piper_process.returncode != 0:
                    logger.error(f"Piper failed: {stderr}")
                    return False

                # Play via mpv with software volume — no system ALSA volume change
                play_process = subprocess.run(
                    [
                        'mpv', '--no-video',
                        '--audio-device=alsa/hw:0,0',
                        '--really-quiet',
                        f'--volume={self.volume}',
                        temp_wav_path
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env
                )

                if play_process.returncode == 0:
                    logger.info("Piper TTS completed successfully")
                else:
                    logger.error(f"mpv TTS playback failed: {play_process.stderr}")
                    return False

            finally:
                # Clean up temporary file
                if os.path.exists(temp_wav_path):
                    os.unlink(temp_wav_path)
            
            return True
                
        except subprocess.TimeoutExpired:
            logger.error("Piper TTS timed out")
            return False
        except FileNotFoundError as e:
            logger.error(f"Command not found: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to speak with Piper: {e}")
            return False
    
    def synthesize(self, text: str) -> bytes | None:
        """Run Piper and return raw WAV bytes without playing them."""
        if not self.piper_path or not self.model_path:
            logger.error("Piper or model not available for synthesize()")
            return None
        try:
            env = os.environ.copy()
            env['XDG_RUNTIME_DIR'] = f'/run/user/{os.getuid()}'
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                temp_path = f.name
            try:
                proc = subprocess.Popen(
                    [self.piper_path, '--model', self.model_path, '--output_file', temp_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True
                )
                _, stderr = proc.communicate(input=text, timeout=30)
                if proc.returncode != 0:
                    logger.error(f"Piper synthesize failed: {stderr}")
                    return None
                with open(temp_path, 'rb') as f:
                    return f.read()
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
        except Exception as e:
            logger.error(f"TTS synthesize error: {e}")
            return None

    def list_models(self) -> list:
        """Return all available Piper voice models as [{name, path}]."""
        locations = [
            os.path.expanduser('~/piper/models'),
            os.path.expanduser('~/piper'),
            './piper/models',
            './models',
            '/usr/share/piper/models',
            '/usr/local/share/piper/models',
        ]
        models = []
        seen = set()
        for location in locations:
            if os.path.isdir(location):
                for root, _, files in os.walk(location):
                    for file in sorted(files):
                        if file.endswith('.onnx'):
                            path = os.path.join(root, file)
                            if path not in seen:
                                seen.add(path)
                                models.append({'name': file[:-5], 'path': path})
        return models

    def set_model(self, model_path: str):
        """Set the voice model to use"""
        if os.path.exists(model_path):
            self.model_path = model_path
            logger.info(f"Voice model set to: {model_path}")
            return True
        else:
            logger.error(f"Voice model not found: {model_path}")
            return False
    
    def set_volume(self, volume: int):
        """Set TTS volume (0-100)"""
        self.volume = max(0, min(100, volume))
        logger.info(f"TTS volume set to: {self.volume}")
    
    def get_model(self):
        """Get current voice model path"""
        return self.model_path