#!/bin/bash
# ============================================================
#  CooperStation — Internet Radio Player for Raspberry Pi
#  One-command installer
#  Usage: bash install.sh
# ============================================================

set -e

REPO_URL="https://github.com/philb1192/Pi-Web-Radio-Station.git"
INSTALL_DIR="$HOME/radio-server"
PIPER_DIR="$HOME/piper"
SERVICE_NAME="cooperstation"

# ── Colours ─────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; exit 1; }
ask()     { echo -e "${BOLD}$*${RESET}"; }

# ── Arch detection ───────────────────────────────────────────
ARCH=$(uname -m)
case "$ARCH" in
    aarch64) PIPER_ARCH="aarch64" ;;
    armv7l)  PIPER_ARCH="armv7l"  ;;
    *)       warn "Unknown architecture $ARCH — Piper TTS may not install correctly." ;;
esac

echo
echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║   🎵  CooperStation Installer            ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo

# ── Optional component prompts ───────────────────────────────
ask "Install Piper TTS (Text-to-Speech)? [Y/n]"
read -r ans_tts; [[ "$ans_tts" =~ ^[Nn] ]] && INSTALL_TTS=0 || INSTALL_TTS=1

ask "Install Spotify Connect (raspotify)? [y/N]"
read -r ans_spot; [[ "$ans_spot" =~ ^[Yy] ]] && INSTALL_SPOTIFY=1 || INSTALL_SPOTIFY=0

ask "Set up as a systemd service (auto-start on boot)? [Y/n]"
read -r ans_svc; [[ "$ans_svc" =~ ^[Nn] ]] && INSTALL_SERVICE=0 || INSTALL_SERVICE=1

ask "Rename this Pi to 'CooperStation'? [Y/n]"
read -r ans_name; [[ "$ans_name" =~ ^[Nn] ]] && RENAME_PI=0 || RENAME_PI=1

echo
info "Starting installation…"
echo

# ── 1. System packages ───────────────────────────────────────
info "Updating package list…"
sudo apt-get update -qq

info "Installing system dependencies…"
sudo apt-get install -y -qq \
    mpv \
    alsa-utils \
    python3 \
    python3-venv \
    python3-pip \
    git \
    curl \
    wget
success "System dependencies installed."

# ── 2. Clone / update repo ───────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Existing installation found — pulling latest changes…"
    git -C "$INSTALL_DIR" pull
else
    info "Cloning CooperStation…"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
success "Repository ready at $INSTALL_DIR."

# ── 3. Python virtual environment ───────────────────────────
info "Creating Python virtual environment…"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet aiohttp
success "Python environment ready."

# ── 4. Default config files ─────────────────────────────────
if [ ! -f "$INSTALL_DIR/stations.json" ]; then
    cp "$INSTALL_DIR/stations.example.json" "$INSTALL_DIR/stations.json"
    info "Created default stations.json"
fi
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    cp "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json"
    info "Created default config.json"
fi

# ── 5. Piper TTS ────────────────────────────────────────────
if [ "$INSTALL_TTS" -eq 1 ]; then
    info "Installing Piper TTS…"
    mkdir -p "$PIPER_DIR/piper" "$PIPER_DIR/models"

    PIPER_RELEASE_URL="https://github.com/rhasspy/piper/releases/latest/download/piper_${PIPER_ARCH}.tar.gz"
    info "Downloading Piper binary for $PIPER_ARCH…"
    if wget -q --show-progress -O /tmp/piper.tar.gz "$PIPER_RELEASE_URL"; then
        tar -xzf /tmp/piper.tar.gz -C "$PIPER_DIR/piper" --strip-components=1
        rm /tmp/piper.tar.gz
        success "Piper binary installed."
    else
        warn "Could not download Piper — TTS will not work. You can install it later from the Settings page."
    fi

    info "Downloading default voice model (en_US-amy-low)…"
    BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en_US/amy/low"
    if wget -q --show-progress -O "$PIPER_DIR/models/en_US-amy-low.onnx" "$BASE/en_US-amy-low.onnx" \
       && wget -q -O "$PIPER_DIR/models/en_US-amy-low.onnx.json" "$BASE/en_US-amy-low.onnx.json"; then
        # Write model path into config.json
        python3 - <<EOF
import json, os
cfg_path = os.path.expanduser('$INSTALL_DIR/config.json')
with open(cfg_path) as f: cfg = json.load(f)
cfg['default_tts_model'] = os.path.expanduser('$PIPER_DIR/models/en_US-amy-low.onnx')
with open(cfg_path, 'w') as f: json.dump(cfg, f, indent=2)
EOF
        success "Voice model installed (en_US-amy-low)."
    else
        warn "Could not download voice model. You can add one later from the Settings page."
    fi
fi

# ── 6. Spotify Connect ──────────────────────────────────────
if [ "$INSTALL_SPOTIFY" -eq 1 ]; then
    info "Installing raspotify (Spotify Connect)…"
    curl -sL https://dtcooper.github.io/raspotify/install.sh | sh

    info "Configuring raspotify…"
    HOSTNAME_PRETTY=$(hostname)
    if [ "$RENAME_PI" -eq 1 ]; then HOSTNAME_PRETTY="CooperStation"; fi

    # Update raspotify config
    sudo sed -i "s|^#*LIBRESPOT_NAME=.*|LIBRESPOT_NAME=\"$HOSTNAME_PRETTY\"|" /etc/raspotify/conf 2>/dev/null || true
    sudo sed -i "s|^#*LIBRESPOT_DEVICE=.*|LIBRESPOT_DEVICE=hw:0,0|" /etc/raspotify/conf 2>/dev/null || true
    sudo sed -i "s|^#*LIBRESPOT_ONEVENT=.*|LIBRESPOT_ONEVENT=$INSTALL_DIR/spotify_event.sh|" /etc/raspotify/conf 2>/dev/null || true

    chmod +x "$INSTALL_DIR/spotify_event.sh"
    sudo systemctl enable raspotify
    sudo systemctl restart raspotify
    success "Spotify Connect (CooperStation) installed."
fi

# ── 7. Rename Pi ────────────────────────────────────────────
if [ "$RENAME_PI" -eq 1 ]; then
    echo 'PRETTY_HOSTNAME=CooperStation' | sudo tee /etc/machine-info > /dev/null
    success "Hostname pretty name set to CooperStation."
fi

# ── 8. Systemd service ──────────────────────────────────────
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    info "Installing systemd service…"
    sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=CooperStation Internet Radio
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/server.py
Restart=on-failure
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"
    success "Service installed and started."
fi

# ── Done ─────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║   ✅  Installation complete!             ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo
IP=$(hostname -I | awk '{print $1}')
echo -e "  Open the radio player in your browser:"
echo -e "  ${BOLD}http://${IP}:8080${RESET}"
echo
if [ "$INSTALL_SERVICE" -eq 0 ]; then
    echo -e "  To start manually:"
    echo -e "  ${CYAN}cd $INSTALL_DIR && source venv/bin/activate && python3 server.py${RESET}"
    echo
fi
echo -e "  User guide: ${BOLD}http://${IP}:8080/help${RESET}"
echo
