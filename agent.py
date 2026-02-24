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
import json
import subprocess
import shutil
import re
import traceback

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
_vms_cache = []
_vms_cache_at = 0.0
_vms_cache_lock = threading.Lock()


def _set_op(name, **fields):
    with _ops_lock:
        current = _ops.get(name, {})
        # If Tart reports only percent but total size is known, derive transferred GB
        # so UI doesn't get stuck at "0.0 / X GB" while percent advances.
        if 'progress_pct' in fields and 'transferred_gb' not in fields:
            total_gb = fields.get('total_gb', current.get('total_gb'))
            progress_pct = fields.get('progress_pct')
            try:
                if total_gb is not None and progress_pct is not None:
                    fields['transferred_gb'] = round((float(total_gb) * float(progress_pct)) / 100.0, 1)
            except (TypeError, ValueError):
                pass
        current.update(fields)
        _ops[name] = current


def _op_is_active(op):
    status = (op or {}).get('status')
    return status not in (None, 'done', 'error', 'idle')


def _has_active_ops():
    with _ops_lock:
        return any(_op_is_active(op) for op in _ops.values())


def _get_vms_snapshot():
    """
    Return VM list while minimizing Tart lock contention.
    During active save/restore operations we serve cached data instead of
    repeatedly calling `tart list` from polling endpoints.
    """
    global _vms_cache, _vms_cache_at
    if _has_active_ops():
        with _vms_cache_lock:
            return list(_vms_cache)

    vms = tart_runner.list_vms()
    with _vms_cache_lock:
        _vms_cache = list(vms)
        _vms_cache_at = time.time()
    return vms


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
    Accept any valid RFB banner. Both Tart's hypervisor VNC (127.0.0.1:5900,
    via Apple Virtualization.framework) and macOS Screen Sharing inside the VM
    present 'RFB 003.889'. Rejecting by version was over-strict and prevented
    all Apple VNC endpoints from being used.
    """
    return bool(banner and banner.startswith('RFB '))


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
    vms = _get_vms_snapshot()
    running = [
        v for v in vms
        if (v.get('state') or v.get('State') or '').lower() == 'running'
    ]
    registry_free_gb, registry_path, registry_probe = _registry_storage_stats()
    cpu_usage_pct = _cpu_usage_pct()
    ram_used_gb, ram_total_gb = _ram_usage_gb()
    net_mbps, net_type, net_if = _network_snapshot()
    return jsonify({
        'status': 'ok',
        'tart_version': _tart_version(),
        'running_vms': len(running),
        'max_vms': agent_config.MAX_VMS,
        'free_slots': max(0, agent_config.MAX_VMS - len(running)),
        'all_vms': len(vms),
        'disk_free_gb': _disk_free_gb(),
        'registry_free_gb': registry_free_gb,
        'registry_path': registry_path,
        'registry_probe': registry_probe,
        'cpu_usage_pct': cpu_usage_pct,
        'ram_used_gb': ram_used_gb,
        'ram_total_gb': ram_total_gb,
        'network_mbps': net_mbps,
        'network_type': net_type,
        'network_interface': net_if,
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
        stat = shutil.disk_usage('/')
        return round(stat.free / 1e9, 1)
    except Exception:
        return None


def _registry_storage_stats():
    """
    Return registry backing storage free space in GB and path metadata.
    Preference order:
    1) REGISTRY_DATA_DIR env override
    2) Docker inspect mount source for REGISTRY_CONTAINER_NAME
    3) host root fallback
    """
    # Explicit override is best when known.
    if agent_config.REGISTRY_DATA_DIR:
        try:
            stat = shutil.disk_usage(agent_config.REGISTRY_DATA_DIR)
            return round(stat.free / 1e9, 1), agent_config.REGISTRY_DATA_DIR, 'env_registry_data_dir'
        except Exception:
            pass

    # Try to discover the host mount path from Docker container metadata.
    try:
        result = subprocess.run(
            ['docker', 'inspect', agent_config.REGISTRY_CONTAINER_NAME],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            payload = json.loads(result.stdout)
            if payload:
                mounts = payload[0].get('Mounts', []) or []
                for mount in mounts:
                    destination = (mount.get('Destination') or '').strip()
                    source = (mount.get('Source') or '').strip()
                    if destination == '/var/lib/registry' and source:
                        stat = shutil.disk_usage(source)
                        return round(stat.free / 1e9, 1), source, 'docker_inspect_mount'
    except Exception:
        pass

    # Fallback when registry path cannot be discovered.
    root_stat = shutil.disk_usage('/')
    return round(root_stat.free / 1e9, 1), '/', 'host_root_fallback'


def _cpu_usage_pct():
    """
    Snapshot overall CPU usage percent from `top` output.
    """
    try:
        proc = subprocess.run(
            ['top', '-l', '1', '-n', '0'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        text = (proc.stdout or '') + '\n' + (proc.stderr or '')
        # Example: "CPU usage: 7.41% user, 8.64% sys, 83.94% idle"
        m = re.search(r'CPU usage:\s*[\d.]+%\s*user,\s*[\d.]+%\s*sys,\s*([\d.]+)%\s*idle', text)
        if not m:
            return None
        idle = float(m.group(1))
        return round(max(0.0, min(100.0, 100.0 - idle)), 1)
    except Exception:
        return None


def _ram_usage_gb():
    """
    Snapshot RAM used/total in GB using vm_stat + hw.memsize.
    """
    try:
        page_size = 4096
        vm = subprocess.run(
            ['vm_stat'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        vm_out = vm.stdout or ''
        m = re.search(r'page size of (\d+) bytes', vm_out)
        if m:
            page_size = int(m.group(1))
        values = {}
        for key, value in re.findall(r'^([^:]+):\s+(\d+)\.', vm_out, flags=re.MULTILINE):
            values[key.strip()] = int(value)

        # Treat active/wired/compressed as used snapshot.
        used_pages = (
            values.get('Pages active', 0)
            + values.get('Pages wired down', 0)
            + values.get('Pages occupied by compressor', 0)
        )
        used_bytes = used_pages * page_size

        memsize = subprocess.run(
            ['sysctl', '-n', 'hw.memsize'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        total_bytes = int((memsize.stdout or '0').strip() or 0)
        if total_bytes <= 0:
            return None, None
        used_gb = round(used_bytes / (1024 ** 3), 1)
        total_gb = round(total_bytes / (1024 ** 3), 1)
        return used_gb, total_gb
    except Exception:
        return None, None


def _default_interface():
    try:
        proc = subprocess.run(
            ['route', '-n', 'get', 'default'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        m = re.search(r'interface:\s+(\S+)', proc.stdout or '')
        return m.group(1) if m else None
    except Exception:
        return None


def _interface_type(ifname):
    if not ifname:
        return None
    # Best-effort mapping from hardware ports.
    try:
        proc = subprocess.run(
            ['networksetup', '-listallhardwareports'],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        blocks = (proc.stdout or '').split('\n\n')
        for block in blocks:
            if f'Device: {ifname}' not in block:
                continue
            if 'Hardware Port: Wi-Fi' in block:
                return 'Wifi'
            if 'Hardware Port: Ethernet' in block:
                return 'Eth'
            m = re.search(r'Hardware Port:\s*(.+)', block)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return ifname


def _interface_bytes(ifname):
    if not ifname:
        return None
    try:
        proc = subprocess.run(
            ['netstat', '-ibn'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        rx = 0
        tx = 0
        for line in (proc.stdout or '').splitlines():
            parts = line.split()
            if len(parts) < 10 or parts[0] != ifname:
                continue
            # Format: Name Mtu Network Address Ipkts Ierrs Opkts Oerrs Coll Drop Ibytes Obytes
            try:
                ib = int(parts[-2])
                ob = int(parts[-1])
            except (ValueError, IndexError):
                continue
            rx = max(rx, ib)
            tx = max(tx, ob)
        if rx == 0 and tx == 0:
            return None
        return rx + tx
    except Exception:
        return None


def _network_snapshot():
    """
    Snapshot network throughput over ~1s on default interface.
    Returns (mbps, type_label, interface_name).
    """
    ifname = _default_interface()
    iface_type = _interface_type(ifname)
    first = _interface_bytes(ifname)
    if first is None:
        return None, iface_type, ifname
    time.sleep(1.0)
    second = _interface_bytes(ifname)
    if second is None or second < first:
        return None, iface_type, ifname
    delta_bytes = second - first
    mbps = round((delta_bytes * 8.0) / 1_000_000.0, 1)
    return mbps, iface_type, ifname


# ── VM list ────────────────────────────────────────────────────────────────────

@app.route('/vms')
def list_vms():
    vms = _get_vms_snapshot()
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
    expected_disk_gb = data.get('expected_disk_gb')

    def _do_save():
        _set_op(
            name,
            op='save',
            status='stopping',
            progress_pct=0,
            transferred_gb=0.0,
            total_gb=expected_disk_gb,
            last_progress_line='Preparing save operation...',
            error=None,
        )
        try:
            tart_runner.stop_vm(name)
            _set_op(name, status='pushing', last_progress_line='Starting registry push...')
            tart_runner.push_vm(
                name,
                registry_tag,
                progress_cb=lambda update: _set_op(name, **update),
            )
            _set_op(name, status='deleting', last_progress_line='Cleaning local VM after push...')
            tart_runner.delete_vm(name)
            _set_op(name, status='done', progress_pct=100, last_progress_line='Save completed.')
        except Exception as e:
            logger.error("save_vm(%s) async error: %s", name, e)
            _set_op(name, op='save', status='error', error=str(e), last_progress_line='Save failed.')

    threading.Thread(target=_do_save, daemon=True).start()
    return jsonify({'status': 'saving', 'poll': f'/vms/{name}/op'})


@app.route('/vms/<name>/restore', methods=['POST'])
def restore_vm(name):
    """Pull from registry + start (async). Poll /vms/<name>/op for progress."""
    data = request.json
    registry_tag = data['registry_tag']
    expected_disk_gb = data.get('expected_disk_gb')

    def _do_restore():
        _set_op(
            name,
            op='restore',
            status='pulling',
            progress_pct=0,
            transferred_gb=0.0,
            total_gb=expected_disk_gb,
            last_progress_line='Preparing restore operation...',
            error=None,
        )
        try:
            tart_runner.pull_vm(
                registry_tag,
                name,
                progress_cb=lambda update: _set_op(name, **update),
            )
            _set_op(name, status='starting', last_progress_line='Starting VM after transfer...')
            tart_runner.start_vm(name)
            _set_op(name, status='done', progress_pct=100, last_progress_line='Restore completed.')
        except Exception as e:
            logger.error("restore_vm(%s) async error: %s", name, e)
            _set_op(name, op='restore', status='error', error=str(e), last_progress_line='Restore failed.')

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
    localhost = '127.0.0.1'

    localhost_reachable = _wait_for_vnc(localhost)
    localhost_banner = _probe_rfb_banner(localhost)
    localhost_supported = _is_supported_rfb_banner(localhost_banner)

    vm_ip_reachable = False
    vm_ip_banner = None
    vm_ip_supported = False
    if ip:
        vm_ip_reachable = _wait_for_vnc(ip)
        vm_ip_banner = _probe_rfb_banner(ip)
        vm_ip_supported = _is_supported_rfb_banner(vm_ip_banner)

    logger.info(
        "vnc_start(%s) probe localhost=%s:%d reachable=%s banner=%r supported=%s; "
        "vm_ip=%s:%d reachable=%s banner=%r supported=%s preference=%s",
        name,
        localhost,
        agent_config.VNC_PORT,
        localhost_reachable,
        localhost_banner,
        localhost_supported,
        ip,
        agent_config.VNC_PORT,
        vm_ip_reachable,
        vm_ip_banner,
        vm_ip_supported,
        agent_config.VNC_TARGET_PREFERENCE,
    )

    target_host = None
    selected_mode = None
    selected_banner = None

    if agent_config.VNC_TARGET_PREFERENCE == 'localhost_first':
        if localhost_supported:
            target_host = localhost
            selected_mode = 'localhost_primary'
            selected_banner = localhost_banner
        elif vm_ip_supported:
            target_host = ip
            selected_mode = 'vm_ip_fallback'
            selected_banner = vm_ip_banner
    else:  # default: vm_ip_first
        if vm_ip_supported:
            target_host = ip
            selected_mode = 'vm_ip_primary'
            selected_banner = vm_ip_banner
        elif localhost_supported:
            target_host = localhost
            selected_mode = 'localhost_fallback'
            selected_banner = localhost_banner
    if not target_host:
        details = [
            f'{localhost}:{agent_config.VNC_PORT} reachable={localhost_reachable} '
            f'banner={localhost_banner or "no-banner"}'
        ]
        if ip:
            details.append(
                f'{ip}:{agent_config.VNC_PORT} reachable={vm_ip_reachable} '
                f'banner={vm_ip_banner or "no-banner"}'
            )
        return jsonify({
            'error': (
                'No reachable VNC endpoint detected on either VM IP or localhost. '
                f'Current preference={agent_config.VNC_TARGET_PREFERENCE}. '
                f'Probe details: {", ".join(details)}'
            )
        }), 400

    try:
        port = vnc.start_proxy(name, target_host)
        logger.info(
            "vnc_start(%s) selected mode=%s target=%s:%d banner=%r websockify_port=%d",
            name,
            selected_mode,
            target_host,
            agent_config.VNC_PORT,
            selected_banner,
            port,
        )
        return jsonify({'port': port, 'vnc_port': agent_config.VNC_PORT})
    except RuntimeError as e:
        logger.error("vnc_start(%s) RuntimeError: %s", name, e, exc_info=True)
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(
            "vnc_start(%s) unexpected error: %s\n%s",
            name,
            e,
            traceback.format_exc(),
            exc_info=True,
        )
        return jsonify({'error': str(e)}), 500


@app.route('/vnc/<name>/stop', methods=['POST'])
def vnc_stop(name):
    vnc.stop_proxy(name)
    return jsonify({'status': 'stopped'})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=agent_config.AGENT_PORT)
