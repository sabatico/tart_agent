"""
TART Agent — HTTP service deployed to each Mac node.
Wraps `tart` CLI commands in a REST API and manages local websockify processes.

Run with:
    python3 agent.py

Or via start_agent.sh which injects environment variables.
"""
import logging
import socket
import threading
import time

from flask import Flask, jsonify, request, abort

import agent_config
import tart_runner
import vnc_manager

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

vnc = vnc_manager.VncManager()

# In-progress async operation tracker: {vm_name: {op, status, progress, error}}
_ops = {}
_ops_lock = threading.Lock()


def _wait_for_vnc(host, timeout=8, interval=0.4):
    """Wait until VNC port is reachable from the agent host."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, agent_config.VNC_PORT), timeout=2):
                return True
        except OSError:
            time.sleep(interval)
    return False


def _probe_rfb_banner(host, timeout=3):
    """
    Probe VNC endpoint and read initial RFB protocol banner.
    Expected form: 'RFB 003.008'
    """
    try:
        with socket.create_connection((host, agent_config.VNC_PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = sock.recv(12)
            if not data:
                return None
            return data.decode(errors='replace').strip()
    except OSError:
        return None


def _is_supported_rfb_banner(banner):
    """
    Accept only noVNC-compatible RFB versions.
    """
    if not banner or not banner.startswith('RFB '):
        return False
    return banner in ('RFB 003.003', 'RFB 003.007', 'RFB 003.008')


# ── Auth ───────────────────────────────────────────────────────────────────────

def _check_auth():
    if agent_config.AGENT_TOKEN:
        token = request.headers.get('Authorization', '')
        if token != f'Bearer {agent_config.AGENT_TOKEN}':
            abort(401)


@app.before_request
def require_auth():
    if request.endpoint != 'health':
        _check_auth()


# ── Health ─────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    vms = tart_runner.list_vms()
    running = [
        v for v in vms
        if (v.get('state') or v.get('State') or '').lower() == 'running'
    ]
    return jsonify({
        'status': 'ok',
        'tart_version': _tart_version(),
        'running_vms': len(running),
        'max_vms': agent_config.MAX_VMS,
        'free_slots': max(0, agent_config.MAX_VMS - len(running)),
        'all_vms': len(vms),
        'disk_free_gb': _disk_free_gb(),
    })


def _tart_version():
    try:
        import subprocess
        result = subprocess.run(['tart', '--version'],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        return 'unknown'


def _disk_free_gb():
    try:
        import shutil
        stat = shutil.disk_usage('/')
        return round(stat.free / 1e9, 1)
    except Exception:
        return None


# ── VM list ────────────────────────────────────────────────────────────────────

@app.route('/vms')
def list_vms():
    vms = tart_runner.list_vms()
    return jsonify(vms)


# ── VM CRUD ────────────────────────────────────────────────────────────────────

@app.route('/vms/create', methods=['POST'])
def create_vm():
    data = request.json
    name = data['name']
    base_image = data['base_image']
    try:
        tart_runner.create_vm(name, base_image)
        return jsonify({'status': 'created', 'name': name})
    except Exception as e:
        logger.error("create_vm(%s) failed: %s", name, e)
        return jsonify({'error': str(e)}), 500


@app.route('/vms/<name>/start', methods=['POST'])
def start_vm(name):
    try:
        tart_runner.start_vm(name)
        return jsonify({'status': 'started'})
    except Exception as e:
        logger.error("start_vm(%s) failed: %s", name, e)
        return jsonify({'error': str(e)}), 500


@app.route('/vms/<name>/stop', methods=['POST'])
def stop_vm(name):
    ok = tart_runner.stop_vm(name)
    if ok:
        return jsonify({'status': 'stopped'})
    return jsonify({'error': 'shutdown timed out'}), 500


@app.route('/vms/<name>/save', methods=['POST'])
def save_vm(name):
    """Shutdown + push to registry (async). Poll /vms/<name>/op for progress."""
    data = request.json
    registry_tag = data['registry_tag']

    def _do_save():
        with _ops_lock:
            _ops[name] = {'op': 'save', 'status': 'stopping', 'progress': 0, 'error': None}
        try:
            tart_runner.stop_vm(name)
            with _ops_lock:
                _ops[name]['status'] = 'pushing'
            tart_runner.push_vm(name, registry_tag)
            with _ops_lock:
                _ops[name]['status'] = 'deleting'
            tart_runner.delete_vm(name)
            with _ops_lock:
                _ops[name]['status'] = 'done'
        except Exception as e:
            logger.error("save_vm(%s) async error: %s", name, e)
            with _ops_lock:
                _ops[name] = {'op': 'save', 'status': 'error', 'error': str(e)}

    threading.Thread(target=_do_save, daemon=True).start()
    return jsonify({'status': 'saving', 'poll': f'/vms/{name}/op'})


@app.route('/vms/<name>/restore', methods=['POST'])
def restore_vm(name):
    """Pull from registry + start (async). Poll /vms/<name>/op for progress."""
    data = request.json
    registry_tag = data['registry_tag']

    def _do_restore():
        with _ops_lock:
            _ops[name] = {'op': 'restore', 'status': 'pulling', 'progress': 0, 'error': None}
        try:
            tart_runner.pull_vm(registry_tag, name)
            with _ops_lock:
                _ops[name]['status'] = 'starting'
            tart_runner.start_vm(name)
            with _ops_lock:
                _ops[name]['status'] = 'done'
        except Exception as e:
            logger.error("restore_vm(%s) async error: %s", name, e)
            with _ops_lock:
                _ops[name] = {'op': 'restore', 'status': 'error', 'error': str(e)}

    threading.Thread(target=_do_restore, daemon=True).start()
    return jsonify({'status': 'restoring', 'poll': f'/vms/{name}/op'})


@app.route('/vms/<name>/op')
def vm_op_status(name):
    """Poll progress of an ongoing async operation."""
    with _ops_lock:
        op = _ops.get(name)
    if op is None:
        return jsonify({'status': 'idle'})
    return jsonify(op)


@app.route('/vms/<name>/ip')
def vm_ip(name):
    ip = tart_runner.get_vm_ip(name)
    return jsonify({'ip': ip})


@app.route('/vms/<name>', methods=['DELETE'])
def delete_vm(name):
    delete_error = None
    try:
        vnc.stop_proxy(name)
    except Exception as e:
        logger.warning("delete_vm(%s) — stop_proxy warning: %s", name, e)

    # Try to stop first so delete works even for currently running VMs.
    try:
        tart_runner.stop_vm(name)
    except Exception as e:
        logger.warning("delete_vm(%s) — stop warning: %s", name, e)

    try:
        tart_runner.delete_vm(name)
    except Exception as e:
        delete_error = e
        logger.error("delete_vm(%s) failed: %s", name, e)

    # Final source of truth: ensure VM is actually absent.
    try:
        remaining = tart_runner.list_vms()
        names = {(v.get('name') or v.get('Name')) for v in remaining}
    except Exception as e:
        logger.error("delete_vm(%s) — post-delete list failed: %s", name, e)
        return jsonify({'error': f'Post-delete verification failed: {e}'}), 500

    if name in names:
        detail = str(delete_error) if delete_error else 'VM still present after delete attempt'
        return jsonify({'error': detail}), 500

    return jsonify({'status': 'deleted'})


# ── VNC ────────────────────────────────────────────────────────────────────────

@app.route('/vnc/<name>/start', methods=['POST'])
def vnc_start(name):
    ip = tart_runner.get_vm_ip(name)
    # Tart VNC is commonly exposed on the node host (localhost), but older
    # setups may target vm_ip:5900. Try host-local first, then vm_ip fallback.
    candidates = ['127.0.0.1']
    if ip:
        candidates.append(ip)

    target_host = None
    selected_banner = None
    for host in candidates:
        if _wait_for_vnc(host):
            banner = _probe_rfb_banner(host)
            logger.info("vnc_start(%s) probe host=%s:%d banner=%r",
                        name, host, agent_config.VNC_PORT, banner)
            if _is_supported_rfb_banner(banner):
                target_host = host
                selected_banner = banner
                break

    if not target_host:
        details = []
        for host in candidates:
            banner = _probe_rfb_banner(host)
            if banner:
                details.append(f'{host}:{agent_config.VNC_PORT} banner={banner}')
            else:
                details.append(f'{host}:{agent_config.VNC_PORT} no-banner')
        return jsonify({
            'error': (
                'No Tart-compatible VNC endpoint found. '
                f'Probe details: {", ".join(details)}'
            )
        }), 400

    try:
        port = vnc.start_proxy(name, target_host)
        logger.info("vnc_start(%s) selected target=%s:%d banner=%r websockify_port=%d",
                    name, target_host, agent_config.VNC_PORT, selected_banner, port)
        return jsonify({'port': port})
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 500


@app.route('/vnc/<name>/stop', methods=['POST'])
def vnc_stop(name):
    vnc.stop_proxy(name)
    return jsonify({'status': 'stopped'})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=agent_config.AGENT_PORT)
