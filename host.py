# -*- coding: utf-8 -*-
import os, re, sys, time, socket, atexit, subprocess
import psutil
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
HOST_DIR = os.path.join(BASE_DIR, 'hosted_files')
os.makedirs(HOST_DIR, exist_ok=True)

running_processes = {}
HOST_PORT = 8082


def port_in_use(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def free_port(port):
    """
    Port ကို သုံးနေတဲ့ process ကို သတ် (self မပါ)
    ⚠️ pkill -f host.py ကို မသုံးရ — ကိုယ်တိုင် သေသွားမယ်
    """
    me = os.getpid()
    killed = []
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr and conn.laddr.port == port and conn.pid:
                if conn.pid == me:
                    continue
                try:
                    p = psutil.Process(conn.pid)
                    name = p.name()
                    cmd = ' '.join(p.cmdline())[:60]
                    # host.py ကိုယ်တိုင် မဖြစ်ရ
                    if 'host.py' in cmd and p.pid == me:
                        continue
                    p.kill()
                    p.wait(timeout=2)
                    killed.append(f"PID {conn.pid} ({name})")
                except Exception:
                    pass
    except Exception:
        pass

    if killed:
        print(f"🔪 Freed port {port}:")
        for k in killed:
            print(f"   • {k}")

    for _ in range(10):
        if not port_in_use(port):
            return True
        time.sleep(0.3)
    return not port_in_use(port)


def is_running(filename):
    info = running_processes.get(filename)
    if info and info['process']:
        if info['process'].poll() is None:
            return True
        running_processes.pop(filename, None)
    return False


def detect_port(script_path):
    try:
        with open(script_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        for p in [r'app\.run\s*\([^)]*port\s*=\s*(\d+)',
                  r'\.run\s*\([^)]*port\s*=\s*(\d+)',
                  r'\bPORT\s*=\s*(\d+)',
                  r'BASE_PORT\s*=\s*.*?"(\d+)"']:
            m = re.search(p, content)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def safe_name(name):
    if not name:
        return None
    name = secure_filename(name)
    if not name or ".." in name:
        return None
    return name


def stop_py_script(filename):
    info = running_processes.get(filename)
    if not info or not info['process']:
        return
    try:
        parent = psutil.Process(info['pid'])
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        parent.kill()
        parent.wait(timeout=3)
    except Exception:
        pass
    try:
        info['log_file'].close()
    except Exception:
        pass
    running_processes.pop(filename, None)


def cleanup_all():
    for name in list(running_processes.keys()):
        stop_py_script(name)


atexit.register(cleanup_all)


# ---------- Routes ----------
@app.route('/')
def serve_ui():
    for name in ('host_ui.html', 'index.html'):
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p):
            return send_file(p)
    return jsonify({"status": "ok", "message": "Host API running", "port": HOST_PORT})


@app.route('/api/files', methods=['GET'])
def list_files():
    files_data = []
    for fname in os.listdir(HOST_DIR):
        if fname.endswith('.log'):
            continue
        ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
        running = is_running(fname) if ext == 'py' else True
        info = running_processes.get(fname)
        files_data.append({
            'name': fname, 'type': ext, 'running': running,
            'pid': info['pid'] if (ext == 'py' and running) else None,
            'port': info.get('port') if (ext == 'py' and running) else None,
            'url': f"/preview/{fname}" if ext in ('html', 'htm') else None,
        })
    return jsonify({'status': 'ok', 'files': files_data, 'host_port': HOST_PORT})


@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['file']
    filename = safe_name(file.filename)
    if not filename:
        return jsonify({'status': 'error', 'message': 'Invalid filename'}), 400
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ('py', 'html', 'htm'):
        return jsonify({'status': 'error', 'message': 'Only .py / .html allowed'}), 400

    save_path = os.path.join(HOST_DIR, filename)
    file.save(save_path)

    note = ''
    auto_started = False
    if ext == 'py':
        if is_running(filename):
            note = ' (already running)'
        else:
            ok, msg = start_py_script(filename)
            auto_started = ok
            note = f' — {msg}' if ok else f' — not started: {msg}'

    return jsonify({
        'status': 'ok',
        'message': f'{filename} uploaded{note}',
        'auto_started': auto_started,
    })


@app.route('/api/rename', methods=['POST'])
def rename_file():
    """ဖိုင်နာမည် ပြောင်းရန် — 'Saver.py' ကို 'main.py' ပြောင်းနိုင်"""
    data = request.get_json(silent=True) or {}
    old = safe_name(data.get('old'))
    new = safe_name(data.get('new'))
    if not old or not new:
        return jsonify({'status': 'error', 'message': 'Invalid names'}), 400

    old_path = os.path.join(HOST_DIR, old)
    new_path = os.path.join(HOST_DIR, new)
    if not os.path.exists(old_path):
        return jsonify({'status': 'error', 'message': 'Source file not found'}), 404
    if os.path.exists(new_path):
        return jsonify({'status': 'error', 'message': 'Target already exists'}), 400

    was_running = is_running(old)
    if was_running:
        stop_py_script(old)

    try:
        os.rename(old_path, new_path)
        old_log = os.path.join(HOST_DIR, f"{old}.log")
        if os.path.exists(old_log):
            try: os.remove(old_log)
            except: pass
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

    return jsonify({'status': 'ok', 'message': f'Renamed {old} → {new}', 'was_running': was_running})


def start_py_script(filename, force=False):
    filename = safe_name(filename)
    if not filename:
        return False, "invalid filename"
    script_path = os.path.join(HOST_DIR, filename)
    if not os.path.exists(script_path):
        return False, "file not found"

    if is_running(filename):
        if not force:
            return False, "already running"
        stop_py_script(filename)
        time.sleep(0.8)

    port = detect_port(script_path)
    if port and port == HOST_PORT:
        return False, f"port {port} is host.py's port"
    if port and port_in_use(port):
        print(f"⚠️ Port {port} in use — freeing...")
        if not free_port(port):
            return False, f"port {port} could not be freed"

    log_path = os.path.join(HOST_DIR, f"{filename}.log")
    log_file = open(log_path, 'w', encoding='utf-8')
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'

    try:
        proc = subprocess.Popen(
            [sys.executable, '-u', script_path],
            cwd=HOST_DIR,
            stdout=log_file, stderr=log_file, env=env,
        )
    except Exception as e:
        log_file.close()
        return False, f"failed to launch: {e}"

    running_processes[filename] = {
        'process': proc, 'pid': proc.pid, 'log_file': log_file,
        'start_time': datetime.now().strftime('%H:%M:%S'),
        'port': port,
    }

    time.sleep(1.5)
    if proc.poll() is not None:
        try: log_file.close()
        except: pass
        err = ""
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                err = f.read()[-500:]
        except: pass
        running_processes.pop(filename, None)
        return False, f"crashed: {err or 'unknown error'}"

    return True, (f"started on port {port}" if port else "started")


@app.route('/api/start/<filename>', methods=['POST'])
def start_file(filename):
    force = request.args.get('force', 'false').lower() == 'true'
    ok, msg = start_py_script(filename, force=force)
    return jsonify({'status': 'ok' if ok else 'error', 'message': msg})


@app.route('/api/restart/<filename>', methods=['POST'])
def restart_file(filename):
    ok, msg = start_py_script(filename, force=True)
    return jsonify({'status': 'ok' if ok else 'error', 'message': msg})


@app.route('/api/stop/<filename>', methods=['POST'])
def stop_file(filename):
    stop_py_script(filename)
    return jsonify({'status': 'ok', 'message': f'Stopped {filename}'})


@app.route('/api/logs/<filename>', methods=['GET'])
def get_logs(filename):
    log_path = os.path.join(HOST_DIR, f"{filename}.log")
    if not os.path.exists(log_path):
        return jsonify({'logs': 'No logs available yet.'})
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()[-4000:]
    return jsonify({'logs': content or '(Log is empty)'})


@app.route('/api/delete/<filename>', methods=['DELETE'])
def delete_file(filename):
    stop_py_script(filename)
    fpath = os.path.join(HOST_DIR, filename)
    lpath = os.path.join(HOST_DIR, f"{filename}.log")
    if os.path.exists(fpath): os.remove(fpath)
    if os.path.exists(lpath): os.remove(lpath)
    return jsonify({'status': 'ok', 'message': f'Deleted {filename}'})


@app.route('/preview/<filename>')
def preview_html(filename):
    return send_from_directory(HOST_DIR, filename)


if __name__ == '__main__':
    print(f"🚀 Shyo Host starting on port {HOST_PORT}...")
    if port_in_use(HOST_PORT):
        print(f"⚠️ Port {HOST_PORT} in use — auto-freeing...")
        if not free_port(HOST_PORT):
            print(f"❌ Could not free port {HOST_PORT}.")
            print(f"   Try: pkill -9 -f 'python host.py'")
            sys.exit(1)

    ui_found = None
    for name in ('host_ui.html', 'index.html'):
        if os.path.exists(os.path.join(BASE_DIR, name)):
            ui_found = name
            break
    if ui_found:
        print(f"✅ UI: {ui_found} → http://127.0.0.1:{HOST_PORT}/")
    else:
        print(f"⚠️  No UI file — put host_ui.html next to host.py")

    print(f"✅ Port {HOST_PORT} is free. Starting...")
    app.run(host='0.0.0.0', port=HOST_PORT,
            debug=False, use_reloader=False, threaded=True)