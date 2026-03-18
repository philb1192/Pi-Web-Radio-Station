let ws;
let state = {};
let defaultStationId = null;
let ttsVolume = 50;
let currentTab = 'tts';
let activePreviewUrl = null;
let activePreviewBtn = null;
let sysinfoInterval = null;
let prevDownloadKeys = [];
let availableModels = [];
let modelBrowserOpen = false;

async function toggleAudioOutput() {
    const next = (state.audio_output || 'pi') === 'pi' ? 'browser' : 'pi';
    await fetch('/api/audio-output', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ output: next })
    });
}

function toggleTheme() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const next = isDark ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    document.getElementById('themeBtn').textContent = next === 'dark' ? '☀️' : '🌙';
}

// Sync button icon with saved theme once DOM is ready; set up preview audio reset
document.addEventListener('DOMContentLoaded', function() {
    if (localStorage.getItem('theme') === 'dark')
        document.getElementById('themeBtn').textContent = '☀️';

    document.getElementById('previewAudio').addEventListener('ended', () => {
        if (activePreviewBtn) {
            activePreviewBtn.textContent = '▶ Preview';
            activePreviewBtn.classList.remove('previewing');
            activePreviewUrl = null;
            activePreviewBtn = null;
        }
    });
});

function showTab(tabName) {
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(tabName + 'Tab').classList.add('active');
    const idx = ['tts', 'stations', 'settings'].indexOf(tabName);
    const btns = document.querySelectorAll('.tab-btn');
    if (btns[idx]) btns[idx].classList.add('active');
    currentTab = tabName;

    clearInterval(sysinfoInterval);
    sysinfoInterval = null;
    if (tabName === 'settings') {
        loadSysInfo();
        sysinfoInterval = setInterval(loadSysInfo, 15000);
    }
}

function connect() {
    const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${wsProto}//${window.location.host}/ws`);
    
    ws.onopen = () => {
        document.getElementById('status').textContent = 'Connected to Radio Server';
        document.getElementById('status').className = 'status connected';
        // Request config after connecting
        fetch('/api/config')
            .then(r => r.json())
            .then(config => {
                defaultStationId = config.default_station_id || null;
                updateState(state); // Refresh UI with default station info
            });
    };
    
    ws.onclose = () => {
        document.getElementById('status').textContent = 'Disconnected - Reconnecting...';
        document.getElementById('status').className = 'status disconnected';
        setTimeout(connect, 1000);
    };
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'state_update') {
            updateState(data.state);
        }
    };
}

function setDefaultStation(stationId) {
    defaultStationId = stationId;
    send({ action: 'set_default_station', station_id: stationId });
}

function updateState(newState) {
    state = newState;
    
    // Update UI
    if (state.current_station) {
        document.getElementById('nowPlaying').textContent = state.current_station.name;
        document.getElementById('playBtn').disabled = false;
    } else {
        document.getElementById('nowPlaying').textContent = 'No station selected';
    }
    
    document.getElementById('playBtn').textContent = state.playing ? '⏸ Pause' : '▶ Play';

    const metaEl = document.getElementById('nowMeta');
    if (state.metadata) {
        metaEl.textContent = state.metadata;
        metaEl.style.display = '';
    } else {
        metaEl.style.display = 'none';
    }

    const reconnectEl = document.getElementById('reconnectStatus');
    if (state.reconnect_status) {
        reconnectEl.textContent = state.reconnect_status;
        reconnectEl.style.display = '';
    } else {
        reconnectEl.style.display = 'none';
    }
    
    const sp = state.spotify_status || {};
    const spEl = document.getElementById('spotifyStatus');
    if (sp.active) {
        const icon = sp.playing ? '▶' : '⏸';
        const track = sp.track || 'Spotify';
        const artist = sp.artist ? ` — ${sp.artist}` : '';
        spEl.textContent = `${icon} ${track}${artist}`;
        spEl.style.display = '';
    } else {
        spEl.style.display = 'none';
    }

    const bt = state.bt_status || {};
    const btEl = document.getElementById('btStatus');
    if (bt.connected) {
        const label = bt.device_name || bt.device_mac || 'Phone';
        let detail;
        if (bt.streaming) {
            detail = 'streaming';
        } else if (bt.idle_secs > 0) {
            const idleMins = Math.floor(bt.idle_secs / 60);
            const remainMins = Math.floor((bt.idle_timeout - bt.idle_secs) / 60);
            detail = `idle ${idleMins} min — auto-off in ${remainMins} min`;
        } else {
            detail = 'connected';
        }
        btEl.textContent = `\uD83D\uDCF1 ${label} — ${detail}`;
        btEl.style.display = '';
    } else {
        btEl.style.display = 'none';
    }

    const queueEl = document.getElementById('ttsQueueInfo');
    const queueSize = state.tts_queue_size || 0;
    if (queueSize > 0) {
        queueEl.textContent = queueSize === 1 ? '1 message queued' : `${queueSize} messages queued`;
        queueEl.style.display = '';
    } else {
        queueEl.style.display = 'none';
    }

    document.getElementById('volume').value = state.volume * 100;
    document.getElementById('volumeValue').textContent = Math.min(100, Math.round(state.volume * 100)) + '%';
    document.getElementById('muteBtn').textContent = state.muted ? '🔇 Unmute' : '🔊 Mute';
    
    // Sync preview button if audio ended (e.g. stream dropped)
    const previewAudio = document.getElementById('previewAudio');
    if (activePreviewBtn && previewAudio && previewAudio.paused && previewAudio.src === '') {
        activePreviewBtn.textContent = '▶ Preview';
        activePreviewBtn.classList.remove('previewing');
        activePreviewUrl = null;
        activePreviewBtn = null;
    }

    // Voice model download progress & completion
    const downloads = state.tts_downloads || {};
    const dlKeys = Object.keys(downloads);
    const justCompleted = prevDownloadKeys.filter(k => !dlKeys.includes(k));
    prevDownloadKeys = dlKeys;
    if (justCompleted.length) {
        // Mark as installed in our cached list and refresh dropdowns
        justCompleted.forEach(k => {
            const m = availableModels.find(x => x.key === k);
            if (m) m.installed = true;
        });
        loadSettings();
    }
    if (modelBrowserOpen) filterAvailableModels();

    // Output toggle button
    const outputBtn = document.getElementById('outputBtn');
    const isBrowser = (state.audio_output || 'pi') === 'browser';
    outputBtn.textContent = isBrowser ? '🖥 Browser' : '🔊 Pi';
    outputBtn.title = isBrowser ? 'Audio: Browser — click to switch to Pi' : 'Audio: Pi — click to switch to Browser';
    outputBtn.classList.toggle('browser-mode', isBrowser);

    // Browser audio element — play stream directly when in browser mode
    const browserAudio = document.getElementById('browserAudio');
    const wantBrowserAudio = isBrowser && state.playing && state.current_station;
    if (wantBrowserAudio) {
        browserAudio.volume = state.volume;
        browserAudio.muted = state.muted;
        const url = state.current_station.url;
        if (browserAudio.dataset.url !== url) {
            browserAudio.src = url;
            browserAudio.dataset.url = url;
            browserAudio.play().catch(() => {});
        } else if (browserAudio.paused) {
            browserAudio.play().catch(() => {});
        }
    } else {
        if (!browserAudio.paused || browserAudio.src) {
            browserAudio.pause();
            browserAudio.src = '';
            browserAudio.dataset.url = '';
        }
    }

    // Update stations
    const stationsDiv = document.getElementById('stations');
    stationsDiv.innerHTML = '';
    state.stations.forEach((station, idx) => {
        const div = document.createElement('div');
        div.className = 'station' + (state.current_station && state.current_station.id === station.id ? ' active' : '');
        const isDefault = defaultStationId === station.id;
        const isFirst = idx === 0;
        const isLast = idx === state.stations.length - 1;
        const health = (state.station_health || {})[station.id];
        const healthClass = health === true ? 'online' : health === false ? 'offline' : 'unknown';
        const healthTitle = health === true ? 'Online' : health === false ? 'Offline' : 'Checking…';
        div.innerHTML = `
            <div class="station-info">
                <span class="station-star ${isDefault ? 'active' : ''}" onclick="event.stopPropagation(); toggleDefaultStation(${station.id})" title="Set as default station">⭐</span>
                <span class="station-id">#${station.id}</span>
                <span class="health-dot ${healthClass}" title="${healthTitle}"></span>
                <span>${station.name}</span>
            </div>
            <div class="station-actions">
                <button class="reorder-btn" onclick="event.stopPropagation(); reorderStation(${station.id}, 'up')" ${isFirst ? 'disabled' : ''} title="Move up">↑</button>
                <button class="reorder-btn" onclick="event.stopPropagation(); reorderStation(${station.id}, 'down')" ${isLast ? 'disabled' : ''} title="Move down">↓</button>
                <button onclick="event.stopPropagation(); deleteStation(${station.id})">Delete</button>
            </div>
        `;
        div.onclick = (e) => {
            if (e.target.tagName !== 'BUTTON' && !e.target.classList.contains('station-star')) {
                playStation(station.id);
            }
        };
        stationsDiv.appendChild(div);
    });
}

function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
    }
}

function playStation(stationId) {
    send({ action: 'play', station_id: stationId });
}

function toggleDefaultStation(stationId) {
    if (defaultStationId === stationId) {
        // Unset default
        setDefaultStation(null);
    } else {
        // Set as default
        setDefaultStation(stationId);
    }
    // Force UI update
    updateState(state);
}

function reorderStation(id, direction) {
    const ids = state.stations.map(s => s.id);
    const idx = ids.indexOf(id);
    if (direction === 'up' && idx > 0) {
        [ids[idx - 1], ids[idx]] = [ids[idx], ids[idx - 1]];
    } else if (direction === 'down' && idx < ids.length - 1) {
        [ids[idx + 1], ids[idx]] = [ids[idx], ids[idx + 1]];
    } else {
        return;
    }
    fetch('/api/stations/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids })
    });
}

function deleteStation(stationId) {
    if (confirm('Delete this station?')) {
        send({ action: 'delete_station', station_id: stationId });
    }
}

function toggleDirectory() {
    const panel = document.getElementById('directoryPanel');
    const arrow = document.getElementById('directoryArrow');
    const open = panel.style.display === 'none';
    panel.style.display = open ? '' : 'none';
    arrow.textContent = open ? '▴' : '▾';
}

async function searchDirectory() {
    const query = document.getElementById('directoryQuery').value.trim();
    if (!query) return;

    const btn = document.getElementById('directorySearchBtn');
    const resultsEl = document.getElementById('directoryResults');
    btn.disabled = true;
    btn.textContent = 'Searching…';
    resultsEl.innerHTML = '';

    try {
        const resp = await fetch('/api/directory/search?q=' + encodeURIComponent(query));
        const data = await resp.json();

        if (data.error) {
            resultsEl.innerHTML = `<p class="directory-msg">Error: ${data.error}</p>`;
            return;
        }
        if (!data.length) {
            resultsEl.innerHTML = '<p class="directory-msg">No stations found.</p>';
            return;
        }

        data.forEach(station => {
            const row = document.createElement('div');
            row.className = 'directory-result';

            const info = document.createElement('div');
            info.className = 'directory-result-info';

            const meta = [station.country, station.language,
                (station.codec && station.bitrate) ? `${station.codec} ${station.bitrate}kbps` : station.codec
            ].filter(Boolean).join(' · ');

            info.innerHTML = `<strong>${station.name}</strong><small>${meta}</small>`;

            const previewBtn = document.createElement('button');
            previewBtn.className = 'directory-preview-btn';
            previewBtn.textContent = activePreviewUrl === station.url ? '■ Stop' : '▶ Preview';
            if (activePreviewUrl === station.url) previewBtn.classList.add('previewing');
            previewBtn.onclick = () => {
                const previewAudio = document.getElementById('previewAudio');
                if (activePreviewUrl === station.url) {
                    previewAudio.pause();
                    previewAudio.src = '';
                    previewBtn.textContent = '▶ Preview';
                    previewBtn.classList.remove('previewing');
                    activePreviewUrl = null;
                    activePreviewBtn = null;
                } else {
                    if (activePreviewBtn) {
                        activePreviewBtn.textContent = '▶ Preview';
                        activePreviewBtn.classList.remove('previewing');
                    }
                    previewAudio.src = station.url;
                    previewAudio.play().catch(() => {});
                    previewBtn.textContent = '■ Stop';
                    previewBtn.classList.add('previewing');
                    activePreviewUrl = station.url;
                    activePreviewBtn = previewBtn;
                }
            };

            const btn2 = document.createElement('button');
            btn2.className = 'directory-add-btn';
            btn2.textContent = 'Add';
            btn2.onclick = () => {
                send({ action: 'add_station', name: station.name, url: station.url });
                btn2.textContent = 'Added';
                btn2.disabled = true;
            };

            row.appendChild(info);
            row.appendChild(previewBtn);
            row.appendChild(btn2);
            resultsEl.appendChild(row);
        });
    } catch (e) {
        resultsEl.innerHTML = `<p class="directory-msg">Search failed: ${e.message}</p>`;
    } finally {
        btn.disabled = false;
        btn.textContent = 'Search';
    }
}

function addStation() {
    const name = document.getElementById('stationName').value;
    const url = document.getElementById('stationUrl').value;
    if (name && url) {
        send({ action: 'add_station', name, url });
        document.getElementById('stationName').value = '';
        document.getElementById('stationUrl').value = '';
    }
}

// Browser-side TTS queue (used when audio output = browser)
let ttsBrowserQueue = [];
let ttsBrowserPlaying = false;

async function _processBrowserTTSQueue() {
    if (ttsBrowserPlaying || ttsBrowserQueue.length === 0) return;
    ttsBrowserPlaying = true;
    const { text, volume } = ttsBrowserQueue.shift();
    updateBrowserTTSQueueInfo();
    try {
        const resp = await fetch('/api/speak-browser', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text })
        });
        if (!resp.ok) throw new Error('TTS request failed');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audio.volume = volume / 100;
        await new Promise(resolve => {
            audio.onended = resolve;
            audio.onerror = resolve;
            audio.play().catch(resolve);
        });
        URL.revokeObjectURL(url);
    } catch (e) {
        console.error('Browser TTS error:', e);
    }
    ttsBrowserPlaying = false;
    updateBrowserTTSQueueInfo();
    _processBrowserTTSQueue();
}

function updateBrowserTTSQueueInfo() {
    const queueEl = document.getElementById('ttsQueueInfo');
    const total = ttsBrowserQueue.length + (ttsBrowserPlaying ? 1 : 0);
    if (total > 0) {
        queueEl.textContent = total === 1 ? '1 message queued' : `${total} messages queued`;
        queueEl.style.display = '';
    } else {
        queueEl.style.display = 'none';
    }
}

async function speakText() {
    const text = document.getElementById('ttsText').value;
    if (!text) {
        alert('Please enter some text to speak');
        return;
    }

    if ((state.audio_output || 'pi') === 'browser') {
        ttsBrowserQueue.push({ text, volume: ttsVolume });
        document.getElementById('ttsText').value = '';
        updateBrowserTTSQueueInfo();
        _processBrowserTTSQueue();
        return;
    }

    try {
        const response = await fetch('/api/speak', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, volume: ttsVolume })
        });
        const result = await response.json();
        if (result.status === 'ok') {
            document.getElementById('ttsText').value = '';
        } else {
            alert('Failed to speak: ' + result.message);
        }
    } catch (error) {
        alert('Failed to speak: ' + error.message);
    }
}

async function exportStations() {
    try {
        const response = await fetch('/api/export');
        const data = await response.json();
        
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'radio_stations.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        window.URL.revokeObjectURL(url);
        
        alert('Stations exported successfully!');
    } catch (error) {
        alert('Failed to export stations: ' + error.message);
    }
}

async function importStations(event) {
    const file = event.target.files[0];
    if (!file) return;
    
    try {
        const text = await file.text();
        const stations = JSON.parse(text);
        
        const response = await fetch('/api/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(stations)
        });
        
        const result = await response.json();
        
        if (result.status === 'ok') {
            alert(result.message);
            window.location.reload();
        } else {
            alert('Import failed: ' + result.message);
        }
    } catch (error) {
        alert('Failed to import stations: ' + error.message);
    }
    
    event.target.value = '';
}

document.getElementById('playBtn').onclick = () => {
    send({ action: 'toggle_play' });
};

let volumeDebounceTimer = null;
document.getElementById('volume').oninput = (e) => {
    const vol = e.target.value / 100;
    document.getElementById('volumeValue').textContent = Math.round(e.target.value) + '%';
    if ((state.audio_output || 'pi') === 'browser') {
        const browserAudio = document.getElementById('browserAudio');
        browserAudio.volume = vol;
        browserAudio.muted = false;
        document.getElementById('muteBtn').textContent = '🔊 Mute';
    } else {
        clearTimeout(volumeDebounceTimer);
        volumeDebounceTimer = setTimeout(() => {
            send({ action: 'set_volume', volume: vol });
        }, 300);
    }
};

document.getElementById('muteBtn').onclick = () => {
    if ((state.audio_output || 'pi') === 'browser') {
        const browserAudio = document.getElementById('browserAudio');
        browserAudio.muted = !browserAudio.muted;
        document.getElementById('muteBtn').textContent = browserAudio.muted ? '🔇 Unmute' : '🔊 Mute';
    } else {
        send({ action: 'toggle_mute' });
    }
};

document.getElementById('ttsVolume').oninput = (e) => {
    ttsVolume = parseInt(e.target.value);
    document.getElementById('ttsVolumeValue').textContent = ttsVolume + '%';
};

// ── Voice model management ───────────────────────────────────────────────

function renderInstalledModels(installed, activeModelPath) {
    const el = document.getElementById('installedModelsList');
    if (!el) return;
    if (!installed.length) {
        el.innerHTML = '<p class="model-none">No models installed.</p>';
        return;
    }
    el.innerHTML = installed.map(m => `
        <div class="model-installed-row">
            <span class="model-name">${m.name}</span>
            ${m.path === activeModelPath ? '<span class="model-badge active-badge">Active</span>' : ''}
            <button class="model-delete-btn" onclick="deleteModel('${m.path}', '${m.name}')"
                    ${m.path === activeModelPath ? 'disabled title="Cannot delete active model"' : ''}>Delete</button>
        </div>`).join('');
}

async function deleteModel(path, name) {
    if (!confirm(`Delete voice model "${name}"?`)) return;
    const resp = await fetch('/api/tts/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path })
    });
    const result = await resp.json();
    if (result.status === 'ok') {
        await loadSettings();
        if (modelBrowserOpen) await loadAvailableModels();
    } else {
        alert('Delete failed: ' + result.message);
    }
}

function toggleModelBrowser() {
    const panel = document.getElementById('modelBrowserPanel');
    const arrow = document.getElementById('modelBrowserArrow');
    modelBrowserOpen = !modelBrowserOpen;
    panel.style.display = modelBrowserOpen ? '' : 'none';
    arrow.textContent = modelBrowserOpen ? '▴' : '▾';
    if (modelBrowserOpen && availableModels.length === 0) loadAvailableModels();
}

async function loadAvailableModels() {
    const listEl = document.getElementById('availableModelsList');
    listEl.innerHTML = '<p class="model-none">Loading from HuggingFace…</p>';
    try {
        const resp = await fetch('/api/tts/available');
        const data = await resp.json();
        if (data.error) { listEl.innerHTML = `<p class="model-none">Error: ${data.error}</p>`; return; }
        availableModels = data;
        filterAvailableModels();
    } catch (e) {
        listEl.innerHTML = `<p class="model-none">Failed to load: ${e.message}</p>`;
    }
}

function filterAvailableModels() {
    const query = (document.getElementById('modelSearchInput')?.value || '').toLowerCase();
    const downloads = state.tts_downloads || {};
    const listEl = document.getElementById('availableModelsList');
    if (!listEl) return;
    const filtered = query
        ? availableModels.filter(m =>
            m.key.toLowerCase().includes(query) ||
            m.language_name.toLowerCase().includes(query) ||
            m.name.toLowerCase().includes(query))
        : availableModels;
    if (!filtered.length) { listEl.innerHTML = '<p class="model-none">No results.</p>'; return; }
    listEl.innerHTML = '';
    filtered.forEach(m => {
        const dl = downloads[m.key];
        const row = document.createElement('div');
        row.className = 'model-available-row';
        row.id = 'model-row-' + m.key;
        const progress = dl
            ? `<div class="model-progress"><div class="model-progress-bar ${dl.error ? 'error' : ''}"
               style="width:${dl.error ? 100 : Math.round(dl.progress * 100)}%"></div></div>`
            : '';
        const action = m.installed
            ? '<span class="model-badge installed-badge">Installed</span>'
            : `<button class="model-download-btn" onclick="downloadModel('${m.key}','${m.onnx_path}','${m.json_path || ''}')">Download</button>`;
        row.innerHTML = `
            <div class="model-available-info">
                <strong>${m.key}</strong>
                <small>${m.language_name} · ${m.quality} · ${m.size_mb} MB</small>
            </div>
            ${dl ? progress : action}`;
        listEl.appendChild(row);
    });
}

async function downloadModel(key, onnxPath, jsonPath) {
    const resp = await fetch('/api/tts/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, onnx_path: onnxPath, json_path: jsonPath || null })
    });
    const result = await resp.json();
    if (result.status !== 'ok') alert('Download failed: ' + result.message);
}

async function exportBackup() {
    try {
        const resp = await fetch('/api/backup');
        const blob = await resp.blob();
        const cd = resp.headers.get('Content-Disposition') || '';
        const match = cd.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : 'cooperstation-backup.json';
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename; a.click();
        URL.revokeObjectURL(url);
    } catch (e) {
        alert('Export failed: ' + e.message);
    }
}

async function importBackup(input) {
    const file = input.files[0];
    if (!file) return;
    input.value = '';
    if (!confirm(`Import backup from "${file.name}"?\n\nThis will REPLACE all current stations and restore settings.`)) return;
    const statusEl = document.getElementById('backupStatus');
    statusEl.textContent = 'Restoring…';
    statusEl.className = 'settings-status';
    try {
        const text = await file.text();
        JSON.parse(text); // validate JSON before sending
        const resp = await fetch('/api/restore', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: text
        });
        const result = await resp.json();
        if (result.status === 'ok') {
            statusEl.textContent = `✓ Restored ${result.stations} stations`;
            statusEl.className = 'settings-status ok';
            await loadSettings();
        } else {
            statusEl.textContent = '✗ ' + result.message;
            statusEl.className = 'settings-status error';
        }
    } catch (e) {
        statusEl.textContent = '✗ ' + (e.message.includes('JSON') ? 'Invalid backup file' : e.message);
        statusEl.className = 'settings-status error';
    }
    setTimeout(() => { statusEl.textContent = ''; statusEl.className = ''; }, 5000);
}

async function loadSettings() {
    try {
        const [configResp, modelsResp] = await Promise.all([
            fetch('/api/config'),
            fetch('/api/tts/models')
        ]);
        const config = await configResp.json();
        const models = await modelsResp.json();

        if (config.default_volume !== undefined) {
            const pct = Math.round(config.default_volume * 100);
            document.getElementById('settingVolume').value = pct;
            document.getElementById('settingVolumeValue').textContent = pct + '%';
        }
        if (config.default_tts_volume !== undefined) {
            const v = config.default_tts_volume;
            document.getElementById('settingTtsVolume').value = v;
            document.getElementById('settingTtsVolumeValue').textContent = v + '%';
            ttsVolume = v;
            document.getElementById('ttsVolume').value = v;
            document.getElementById('ttsVolumeValue').textContent = v + '%';
        }
        if (config.default_audio_output) {
            document.getElementById('settingAudioOutput').value = config.default_audio_output;
        }
        if (config.bluetooth_name !== undefined)
            document.getElementById('settingBluetoothName').value = config.bluetooth_name;
        if (config.spotify_name !== undefined)
            document.getElementById('settingSpotifyName').value = config.spotify_name;

        const modelSelect = document.getElementById('settingTtsModel');
        modelSelect.innerHTML = '';
        if (models.length === 0) {
            modelSelect.innerHTML = '<option value="">No models found</option>';
        } else {
            models.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m.path;
                opt.textContent = m.name;
                if (m.path === config.default_tts_model) opt.selected = true;
                modelSelect.appendChild(opt);
            });
        }

        // Refresh installed models management list
        const activeModel = modelSelect.value;
        renderInstalledModels(models, activeModel);
    } catch (e) {
        console.error('Failed to load settings:', e);
    }
}

async function saveSettings() {
    const settings = {
        default_volume: document.getElementById('settingVolume').value / 100,
        default_tts_volume: parseInt(document.getElementById('settingTtsVolume').value),
        default_audio_output: document.getElementById('settingAudioOutput').value,
        default_tts_model: document.getElementById('settingTtsModel').value,
        bluetooth_name: document.getElementById('settingBluetoothName').value.trim(),
        spotify_name: document.getElementById('settingSpotifyName').value.trim(),
    };
    const statusEl = document.getElementById('settingsSaveStatus');
    try {
        const resp = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(settings)
        });
        const result = await resp.json();
        if (result.status === 'ok') {
            // Apply TTS volume immediately in the TTS tab
            ttsVolume = settings.default_tts_volume;
            document.getElementById('ttsVolume').value = ttsVolume;
            document.getElementById('ttsVolumeValue').textContent = ttsVolume + '%';
            statusEl.textContent = '✓ Saved';
            statusEl.className = 'settings-status ok';
        } else {
            statusEl.textContent = '✗ ' + result.message;
            statusEl.className = 'settings-status error';
        }
    } catch (e) {
        statusEl.textContent = '✗ Failed';
        statusEl.className = 'settings-status error';
    }
    setTimeout(() => { statusEl.textContent = ''; statusEl.className = ''; }, 3000);
}

function _sysinfoBar(barId, pct) {
    const bar = document.getElementById(barId);
    if (!bar) return;
    bar.style.width = Math.min(pct, 100) + '%';
    bar.style.background = pct < 60 ? '#4caf50' : pct < 80 ? '#ff9800' : '#f44336';
}

function _formatBytes(bytes) {
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
    if (bytes >= 1e6) return (bytes / 1e6).toFixed(0) + ' MB';
    return (bytes / 1e3).toFixed(0) + ' KB';
}

function _formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const parts = [];
    if (d) parts.push(d + 'd');
    if (h) parts.push(h + 'h');
    parts.push(m + 'm');
    return parts.join(' ');
}

async function loadSysInfo() {
    try {
        const resp = await fetch('/api/sysinfo');
        const info = await resp.json();

        if (info.cpu_temp !== null) {
            const pct = Math.round((info.cpu_temp / 85) * 100);
            _sysinfoBar('sysinfoCpuBar', pct);
            document.getElementById('sysinfoCpuTemp').textContent = info.cpu_temp + '°C';
        }
        if (info.mem_percent !== null) {
            _sysinfoBar('sysinfoMemBar', info.mem_percent);
            document.getElementById('sysinfoMem').textContent =
                _formatBytes(info.mem_used) + ' / ' + _formatBytes(info.mem_total) + ' (' + info.mem_percent + '%)';
        }
        if (info.disk_percent !== null) {
            _sysinfoBar('sysinfoDiskBar', info.disk_percent);
            document.getElementById('sysinfoDisk').textContent =
                _formatBytes(info.disk_used) + ' / ' + _formatBytes(info.disk_total) + ' (' + info.disk_percent + '%)';
        }
        if (info.cpu_load !== null)
            document.getElementById('sysinfoLoad').textContent = info.cpu_load.toFixed(2);
        if (info.uptime_seconds !== null)
            document.getElementById('sysinfoUptime').textContent = _formatUptime(info.uptime_seconds);
    } catch (e) {
        console.error('Failed to load sysinfo:', e);
    }
}

connect();
loadSettings();