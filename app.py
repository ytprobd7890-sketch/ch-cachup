import os
import time
import datetime
import requests
import gzip
import json
import shutil
import xml.etree.ElementTree as ET
from datetime import timezone, timedelta
from flask import Flask, render_template_string, send_from_directory, jsonify, request, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

CONFIG_PATH = "config.json"

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "channel_name": "Zee Bangla HD",
        "stream_url": "http://line.umetop.pro:80/play/live.php?mac=00:1A:79:8F:BA:8A&stream=225796&extension=m3u8",
        "record_interval_seconds": 10,
        "retention_days": 7,
        "timezone_offset_hours": 6,
        "target_epg_channels": ["625", "1977", "0-9-zeebangla", "0-9-9z5383484"],
        "custom_show_aliases": {},
        "custom_catchup_schedules": [],
        "vikingfile_api_key": "",
        "vikingfile_user_hash": "",
        "auto_upload_cloud": False
    }

def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

config = load_config()
RECORD_DIR = "zee_bangla_archives"
os.makedirs(RECORD_DIR, exist_ok=True)
dhaka_tz = timezone(timedelta(hours=config.get("timezone_offset_hours", 6)))

STATS = {
    "total_segments_recorded": 0,
    "last_record_time": "Never",
    "status": "Running",
    "current_show": "Loading EPG...",
    "last_upload_status": "Idle"
}

def upload_to_vikingfile(filepath):
    cfg = load_config()
    api_key = cfg.get("vikingfile_api_key", "").strip()
    user_hash = cfg.get("vikingfile_user_hash", "").strip()
    
    if not api_key:
        return False, "API Key (Key) missing"
        
    try:
        # Step 1: Get upload server from VikingFile API
        server_resp = requests.get("https://vikingfile.com/api/get-server", timeout=10)
        if server_resp.status_code == 200:
            upload_server = server_resp.json().get("server", "https://upload.vikingfile.com")
        else:
            upload_server = "https://upload.vikingfile.com"
            
        # Step 2: Upload file via multipart POST
        with open(filepath, "rb") as f:
            files = {"file": (os.path.basename(filepath), f)}
            data = {
                "key": api_key,
                "user": user_hash
            }
            resp = requests.post(upload_server, data=data, files=files, timeout=120)
            if resp.status_code == 200:
                res_json = resp.json()
                file_url = res_json.get("url", "Uploaded successfully")
                return True, file_url
            else:
                return False, f"HTTP Error {resp.status_code}: {resp.text}"
    except Exception as e:
        return False, str(e)

def get_current_program_info():
    cfg = load_config()
    now_dhaka = datetime.datetime.now(dhaka_tz)
    default_name = f"{cfg['channel_name'].replace(' ', '')}_{now_dhaka.strftime('%Y-%m-%d_%H-%M-%S')}(Asia_Dhaka).mp4"
    default_title = cfg['channel_name']
    
    epg_path = "/tmp/epg.xml.gz"
    if not os.path.exists(epg_path):
        try:
            urllib_url = "https://mitthu786.github.io/tvepg/epg.xml.gz"
            import urllib.request
            urllib.request.urlretrieve(urllib_url, epg_path)
        except:
            return default_title, default_name
            
    try:
        with gzip.open(epg_path, "rb") as f:
            target_channels = set(cfg.get("target_epg_channels", ["625", "1977", "0-9-zeebangla", "0-9-9z5383484"]))
            aliases = cfg.get("custom_show_aliases", {})
            
            for event, elem in ET.iterparse(f, events=("end",)):
                if elem.tag == "programme":
                    channel = elem.get("channel")
                    if channel in target_channels:
                        start_str = elem.get("start", "")
                        stop_str = elem.get("stop", "")
                        try:
                            start_utc = datetime.datetime.strptime(start_str.split()[0][:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                            stop_utc = datetime.datetime.strptime(stop_str.split()[0][:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                            
                            start_dhaka = start_utc.astimezone(dhaka_tz)
                            stop_dhaka = stop_utc.astimezone(dhaka_tz)
                            
                            if start_dhaka <= now_dhaka <= stop_dhaka:
                                title_elem = elem.find("title")
                                title = title_elem.text if title_elem is not None else cfg['channel_name']
                                
                                for key, alias in aliases.items():
                                    if key.lower() in title.lower():
                                        title = alias
                                        break
                                        
                                sanitized = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip()
                                time_str = start_dhaka.strftime("%Y-%m-%d_%H-%M")
                                duration_min = int((stop_dhaka - start_dhaka).total_seconds() / 60)
                                elem.clear()
                                return f"{title} ({duration_min}m EPG)", f"{sanitized}_{time_str}(Asia_Dhaka).mp4"
                        except:
                            pass
                elem.clear()
    except Exception as e:
        pass
        
    return default_title, default_name

def record_chunk():
    global STATS
    cfg = load_config()
    try:
        title, target_filename = get_current_program_info()
        STATS["current_show"] = title
        chunk_file = os.path.join(RECORD_DIR, target_filename)
        
        headers = {"User-Agent": "VLC/3.0.16"}
        resp = requests.get(cfg["stream_url"], headers=headers, timeout=15)
        if resp.status_code == 200:
            lines = resp.text.splitlines()
            ts_urls = [line.strip() for line in lines if line and not line.startswith("#")]
            if ts_urls:
                base_match = cfg["stream_url"].rsplit('/', 1)[0]
                latest_segment = ts_urls[-1]
                if not latest_segment.startswith("http"):
                    latest_segment = f"{base_match}/{latest_segment}"
                    
                seg_resp = requests.get(latest_segment, headers=headers, timeout=10)
                if seg_resp.status_code == 200:
                    with open(chunk_file, "ab") as out:
                        out.write(seg_resp.content)
                    STATS["total_segments_recorded"] += 1
                    STATS["last_record_time"] = datetime.datetime.now(dhaka_tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        STATS["status"] = f"Warning: {str(e)}"

def cleanup_old_files():
    cfg = load_config()
    retention = cfg.get("retention_days", 7)
    now = time.time()
    cutoff = now - (retention * 86400)
    for f in os.listdir(RECORD_DIR):
        fp = os.path.join(RECORD_DIR, f)
        if os.path.isfile(fp):
            if os.path.getmtime(fp) < cutoff:
                try:
                    os.remove(fp)
                except:
                    pass

scheduler = BackgroundScheduler()
scheduler.add_job(func=record_chunk, trigger="interval", seconds=config.get("record_interval_seconds", 10), id="record_job")
scheduler.add_job(func=cleanup_old_files, trigger="interval", hours=6)
scheduler.start()

PRO_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>{{ config.channel_name }} - Pro Cloud Catchup Server</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { background: #0f172a; color: #f8fafc; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; text-align: center; }
        .container { max-width: 950px; margin: 0 auto; }
        h1 { color: #f43f5e; margin-bottom: 5px; }
        .subtitle { color: #94a3b8; margin-bottom: 25px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }
        .stat-card { background: #1e293b; padding: 20px; border-radius: 12px; border: 1px solid #334155; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
        .stat-title { font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }
        .stat-value { font-size: 18px; font-weight: bold; color: #38bdf8; margin-top: 8px; }
        .nav-tabs { display: flex; justify-content: center; gap: 15px; margin-bottom: 20px; flex-wrap: wrap; }
        .tab-btn { background: #1e293b; border: 1px solid #334155; color: #f8fafc; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; }
        .tab-btn.active { background: #f43f5e; border-color: #f43f5e; }
        .panel { background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; text-align: left; display: none; }
        .panel.active { display: block; }
        input[type="text"], input[type="number"], select, textarea { width: 100%; padding: 12px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #fff; margin-bottom: 15px; box-sizing: border-box; }
        textarea { height: 100px; font-family: monospace; }
        ul { list-style: none; padding: 0; margin: 0; max-height: 450px; overflow-y: auto; }
        li { background: #0f172a; margin: 10px 0; padding: 15px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; border: 1px solid #334155; flex-wrap: wrap; gap: 10px; }
        .file-name { font-weight: 500; font-size: 14px; word-break: break-all; margin-right: 15px; }
        .btn-group { display: flex; gap: 10px; flex-shrink: 0; }
        .btn { padding: 8px 14px; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: bold; cursor: pointer; display: inline-block; border: none; }
        .btn-play { background: #0284c7; color: #fff; }
        .btn-play:hover { background: #0369a1; }
        .btn-download { background: #16a34a; color: #fff; }
        .btn-download:hover { background: #15803d; }
        .btn-upload { background: #8b5cf6; color: #fff; }
        .btn-upload:hover { background: #7c3aed; }
        .btn-delete { background: #dc2626; color: #fff; }
        .btn-delete:hover { background: #b91c1c; }
        .btn-save { background: #f43f5e; color: #fff; width: 100%; padding: 12px; font-size: 15px; }
        .btn-save:hover { background: #e11d48; }
        /* Modal Video Player */
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); justify-content: center; align-items: center; }
        .modal-content { background: #1e293b; padding: 20px; border-radius: 12px; width: 80%; max-width: 800px; position: relative; }
        .close-btn { position: absolute; right: 15px; top: 10px; font-size: 24px; cursor: pointer; color: #fff; }
        video { width: 100%; border-radius: 8px; outline: none; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎬 {{ config.channel_name }} Pro Cloud Server</h1>
        <div class="subtitle">24x7 EPG Catchup & VikingFile Cloud API Integration</div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-title">Server Status</div>
                <div class="stat-value" style="color: #4ade80;">🟢 Active (24x7)</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Current On-Air Show</div>
                <div class="stat-value" id="current-show">{{ stats.current_show }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Cloud Upload Status</div>
                <div class="stat-value" style="font-size: 14px;" id="upload-status">{{ stats.last_upload_status }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Storage Used / Free</div>
                <div class="stat-value">{{ disk_usage }}</div>
            </div>
        </div>

        <div class="nav-tabs">
            <button class="tab-btn active" onclick="switchTab('archive')">📁 Recorded Archive</button>
            <button class="tab-btn" onclick="switchTab('cloud')">☁️ VikingFile API</button>
            <button class="tab-btn" onclick="switchTab('settings')">⚙️ Stream Settings</button>
        </div>

        <!-- Archive Panel -->
        <div id="archive-panel" class="panel active">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; flex-wrap: wrap; gap: 10px;">
                <h3>Recorded Programs Archive</h3>
                <span style="color: #94a3b8; font-size: 13px;">Timezone: Asia/Dhaka (UTC+6)</span>
            </div>
            <input type="text" id="searchBox" placeholder="🔍 Search programs by name or date..." onkeyup="filterFiles()">
            <ul id="fileList">
                {% for file in files %}
                <li class="file-item">
                    <span class="file-name">{{ file }}</span>
                    <div class="btn-group">
                        <button class="btn btn-play" onclick="playVideo('{{ file }}')">▶ Play</button>
                        <a href="/download/{{ file }}?download=1" class="btn btn-download">⬇ Download</a>
                        <a href="/upload-cloud/{{ file }}" class="btn btn-upload">☁️ Upload</a>
                        <a href="/delete/{{ file }}" class="btn btn-delete" onclick="return confirm('Delete this recording?');">🗑️ Delete</a>
                    </div>
                </li>
                {% endfor %}
            </ul>
        </div>

        <!-- Cloud API Panel -->
        <div id="cloud-panel" class="panel">
            <h3>☁️ VikingFile Cloud API Integration</h3>
            <p style="color: #94a3b8; font-size: 14px; margin-bottom: 15px;">Configure your API Key and User Hash for vikingfile.com/api automated backups.</p>
            <form action="/save-cloud" method="POST">
                <label>VikingFile API Key (Key):</label>
                <input type="text" name="vikingfile_api_key" value="{{ config.vikingfile_api_key }}" placeholder="e.g. rZ2h9ZqVQi">
                
                <label>VikingFile User Hash (Optional):</label>
                <input type="text" name="vikingfile_user_hash" value="{{ config.vikingfile_user_hash }}" placeholder="Leave empty for anonymous upload">
                
                <label style="display: flex; align-items: center; gap: 10px; cursor: pointer; margin-bottom: 20px;">
                    <input type="checkbox" name="auto_upload_cloud" {% if config.auto_upload_cloud %}checked{% endif %} style="width: 20px; height: 20px;">
                    <span>Enable Auto-Upload to VikingFile Cloud</span>
                </label>
                
                <button type="submit" class="btn btn-save">💾 Save Cloud API Settings</button>
            </form>
        </div>

        <!-- Settings Panel -->
        <div id="settings-panel" class="panel">
            <h3>⚙️ Stream & Retention Settings</h3>
            <form action="/save-settings" method="POST">
                <label>Channel Name:</label>
                <input type="text" name="channel_name" value="{{ config.channel_name }}">
                
                <label>Stream URL (M3U8 / TS):</label>
                <input type="text" name="stream_url" value="{{ config.stream_url }}">
                
                <label>Recording Interval (Seconds):</label>
                <input type="number" name="record_interval_seconds" value="{{ config.record_interval_seconds }}">
                
                <label>Auto-Delete Retention (Days):</label>
                <input type="number" name="retention_days" value="{{ config.retention_days }}">
                
                <button type="submit" class="btn btn-save">💾 Save & Apply Settings</button>
            </form>
        </div>
    </div>

    <!-- Video Player Modal -->
    <div id="playerModal" class="modal">
        <div class="modal-content">
            <span class="close-btn" onclick="closeModal()">&times;</span>
            <h3 id="modalTitle" style="color: #38bdf8; margin-top: 0;">Now Playing</h3>
            <video id="videoPlayer" controls autoplay></video>
        </div>
    </div>

    <script>
        function switchTab(tab) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            if(tab === 'archive') {
                document.querySelectorAll('.tab-btn')[0].classList.add('active');
                document.getElementById('archive-panel').classList.add('active');
            } else if(tab === 'cloud') {
                document.querySelectorAll('.tab-btn')[1].classList.add('active');
                document.getElementById('cloud-panel').classList.add('active');
            } else {
                document.querySelectorAll('.tab-btn')[2].classList.add('active');
                document.getElementById('settings-panel').classList.add('active');
            }
        }

        function filterFiles() {
            let input = document.getElementById('searchBox').value.toLowerCase();
            let items = document.getElementsByClassName('file-item');
            for (let i = 0; i < items.length; i++) {
                let name = items[i].getElementsByClassName('file-name')[0].innerText.toLowerCase();
                if (name.includes(input)) {
                    items[i].style.display = "flex";
                } else {
                    items[i].style.display = "none";
                }
            }
        }

        function playVideo(filename) {
            let modal = document.getElementById('playerModal');
            let video = document.getElementById('videoPlayer');
            let title = document.getElementById('modalTitle');
            title.innerText = filename;
            video.src = "/download/" + filename;
            modal.style.display = "flex";
            video.play();
        }

        function closeModal() {
            let modal = document.getElementById('playerModal');
            let video = document.getElementById('videoPlayer');
            video.pause();
            video.src = "";
            modal.style.display = "none";
        }

        window.onclick = function(event) {
            let modal = document.getElementById('playerModal');
            if (event.target == modal) {
                closeModal();
            }
        }

        setInterval(() => {
            fetch('/api/stats')
                .then(res => res.json())
                .then(data => {
                    document.getElementById('current-show').innerText = data.current_show;
                    document.getElementById('upload-status').innerText = data.last_upload_status;
                });
        }, 15000);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    cfg = load_config()
    files = sorted([f for f in os.listdir(RECORD_DIR) if f.endswith(".mp4") or f.endswith(".ts")], reverse=True)
    
    total, used, free = shutil.disk_usage(RECORD_DIR)
    used_gb = used / (1024**3)
    free_gb = free / (1024**3)
    disk_str = f"{used_gb:.1f} GB Used / {free_gb:.1f} GB Free"
    
    return render_template_string(PRO_HTML_TEMPLATE, files=files, stats=STATS, config=cfg, disk_usage=disk_str)

@app.route("/api/stats")
def api_stats():
    return jsonify(STATS)

@app.route("/save-settings", methods=["POST"])
def save_settings():
    cfg = load_config()
    cfg["channel_name"] = request.form.get("channel_name", cfg["channel_name"])
    cfg["stream_url"] = request.form.get("stream_url", cfg["stream_url"])
    cfg["record_interval_seconds"] = int(request.form.get("record_interval_seconds", cfg["record_interval_seconds"]))
    cfg["retention_days"] = int(request.form.get("retention_days", cfg["retention_days"]))
    save_config(cfg)
    
    try:
        scheduler.reschedule_job("record_job", trigger="interval", seconds=cfg["record_interval_seconds"])
    except:
        pass
    return redirect(url_for("index"))

@app.route("/save-cloud", methods=["POST"])
def save_cloud():
    cfg = load_config()
    cfg["vikingfile_api_key"] = request.form.get("vikingfile_api_key", "").strip()
    cfg["vikingfile_user_hash"] = request.form.get("vikingfile_user_hash", "").strip()
    cfg["auto_upload_cloud"] = True if request.form.get("auto_upload_cloud") else False
    save_config(cfg)
    return redirect(url_for("index"))

@app.route("/upload-cloud/<path:filename>")
def upload_cloud_manual(filename):
    fp = os.path.join(RECORD_DIR, filename)
    if os.path.exists(fp):
        success, msg = upload_to_vikingfile(fp)
        STATS["last_upload_status"] = f"Success: {msg}" if success else f"Failed: {msg}"
    return redirect(url_for("index"))

@app.route("/delete/<path:filename>")
def delete_file(filename):
    fp = os.path.join(RECORD_DIR, filename)
    if os.path.exists(fp):
        try:
            os.remove(fp)
        except:
            pass
    return redirect(url_for("index"))

@app.route("/download/<path:filename>")
def download(filename):
    as_attachment = 'download' in request.args
    return send_from_directory(RECORD_DIR, filename, as_attachment=as_authorization := as_attachment)

if __name__ == "__main__":
    try:
        scheduler.add_job(func=record_chunk, trigger="interval", seconds=config.get("record_interval_seconds", 10), id="record_job")
    except:
        pass
        
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
