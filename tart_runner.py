import json
import logging
import os
import re
import signal
import subprocess
import time
import socket
from urllib.parse import urlparse
import agent_config

logger = logging.getLogger(__name__)


_TRANSFER_RE = re.compile(
    r'(?P<current>\d+(?:\.\d+)?)\s*(?P<cur_unit>GiB|GB|MiB|MB)\s*'
    r'(?:/|of)\s*'
    r'(?P<total>\d+(?:\.\d+)?)\s*(?P<tot_unit>GiB|GB|MiB|MB)',
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r'(?P<pct>\d{1,3})\s*%')


def _to_gb(value, unit):
    unit_l = (unit or '').lower()
    if unit_l in ('gib', 'gb'):
        return float(value)
    if unit_l in ('mib', 'mb'):
        return float(value) / 1024.0
    return float(value)


def _extract_progress(text):
    payload = {}
    if not text:
        return payload
    transfer = _TRANSFER_RE.search(text)
    if transfer:
        payload['transferred_gb'] = round(
            _to_gb(transfer.group('current'), transfer.group('cur_unit')), 1
        )
        payload['total_gb'] = round(
            _to_gb(transfer.group('total'), transfer.group('tot_unit')), 1
        )
        if payload['total_gb'] > 0:
            payload['progress_pct'] = max(
                0,
                min(100, int(round((payload['transferred_gb'] / payload['total_gb']) * 100))),
            )
    percent = _PERCENT_RE.search(text)
    if percent and 'progress_pct' not in payload:
        payload['progress_pct'] = max(0, min(100, int(percent.group('pct'))))
    return payload


def _notify_progress(progress_cb, line, parsed):
    if not progress_cb:
        return
    payload = {'last_progress_line': (line or '').strip()[:300]}
    payload.update(parsed or {})
    progress_cb(payload)


def _log_tart_process_snapshot(context):
    """
    Log active tart processes to diagnose lock waits.
    """
    try:
        proc = subprocess.run(
            ['ps', '-ax', '-o', 'pid,ppid,etime,command'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        lines = [line for line in (proc.stdout or '').splitlines() if 'tart ' in line]
        if not lines:
            logger.warning("%s: no active tart processes found in ps snapshot", context)
            return
        logger.warning("%s: active tart processes:\n%s", context, '\n'.join(lines[:20]))
    except Exception as e:
        logger.warning("%s: failed to capture tart process snapshot: %s", context, e)


def _kill_stale_tart_pulls(registry_tag):
    """
    Find and SIGTERM any existing `tart pull <registry_tag>` processes.
    Orphaned pulls from prior failed/abandoned attempts hold Tart's per-image
    file lock and block new pulls indefinitely.  We kill them before each new
    pull attempt so the lock is released immediately.
    """
    current_pid = os.getpid()
    killed_pids = []
    try:
        proc = subprocess.run(
            ['ps', '-ax', '-o', 'pid=,command='],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in (proc.stdout or '').splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            cmd = parts[1]
            if pid == current_pid:
                continue
            if 'tart' in cmd and 'pull' in cmd and registry_tag in cmd:
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed_pids.append(pid)
                    logger.warning(
                        "kill_stale_tart_pulls: SIGTERM → pid=%d  cmd=%r",
                        pid,
                        cmd[:120],
                    )
                except ProcessLookupError:
                    pass  # already gone
                except PermissionError:
                    logger.warning(
                        "kill_stale_tart_pulls: no permission to kill pid=%d", pid
                    )
    except Exception as e:
        logger.warning("kill_stale_tart_pulls: error scanning processes: %s", e)

    if killed_pids:
        logger.warning(
            "kill_stale_tart_pulls: killed %d stale pull(s) %s — sleeping 2s for lock release",
            len(killed_pids),
            killed_pids,
        )
        time.sleep(2)
    else:
        logger.info("kill_stale_tart_pulls: no stale pulls found for %s", registry_tag)
    return killed_pids


def _run(args, timeout=30, check=True):
    """Run a tart CLI command. Returns stdout string."""
    cmd = ['tart'] + args
    logger.debug("Running: %s", ' '.join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"tart {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _run_with_progress(args, timeout=30, progress_cb=None):
    """
    Run a tart command and emit lightweight progress callbacks from output.
    """
    cmd = ['tart'] + args
    logger.debug("Running with progress: %s", ' '.join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )

    stdout_fd = proc.stdout.fileno() if proc.stdout else None
    stderr_fd = proc.stderr.fileno() if proc.stderr else None
    for fd in (stdout_fd, stderr_fd):
        if fd is not None:
            os.set_blocking(fd, False)

    stdout_chunks = []
    stderr_chunks = []
    buffers = {'stdout': '', 'stderr': ''}
    lock_wait_started_at = None
    last_lock_snapshot_at = 0.0
    while True:
        for stream_name, fd in (('stdout', stdout_fd), ('stderr', stderr_fd)):
            if fd is None:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                chunk = b''
            if not chunk:
                continue
            text = chunk.decode(errors='replace')
            if stream_name == 'stdout':
                stdout_chunks.append(text)
            else:
                stderr_chunks.append(text)
            buffers[stream_name] += text
            while True:
                buffer = buffers[stream_name]
                separators = [i for i in (buffer.find('\r'), buffer.find('\n')) if i >= 0]
                if not separators:
                    break
                idx = min(separators)
                line = buffer[:idx].strip()
                buffers[stream_name] = buffer[idx + 1:]
                if line and progress_cb:
                    progress_cb(line, _extract_progress(line))
                if line and 'waiting for lock' in line.lower():
                    now = time.time()
                    if lock_wait_started_at is None:
                        lock_wait_started_at = now
                    wait_secs = int(now - lock_wait_started_at)
                    # Snapshot tart process tree at first lock wait and every 30s after.
                    if last_lock_snapshot_at == 0.0 or (now - last_lock_snapshot_at) >= 30:
                        _log_tart_process_snapshot(
                            f'tart {" ".join(args[:2])} lock-wait {wait_secs}s'
                        )
                        last_lock_snapshot_at = now

        if proc.poll() is not None:
            break
        time.sleep(0.2)

    if proc.stdout:
        stdout_bytes = proc.stdout.read() or b''
    else:
        stdout_bytes = b''
    if proc.stderr:
        stderr_bytes = proc.stderr.read() or b''
    else:
        stderr_bytes = b''

    stdout = (''.join(stdout_chunks) + stdout_bytes.decode(errors='replace')).strip()
    stderr = (''.join(stderr_chunks) + stderr_bytes.decode(errors='replace')).strip()
    for remainder in buffers.values():
        if remainder.strip() and progress_cb:
            line = remainder.strip()
            progress_cb(line, _extract_progress(line))
    if proc.returncode != 0:
        raise RuntimeError(f"tart {args[0]} failed: {stderr}")
    return stdout


def _registry_host_port_from_tag(registry_tag):
    first = (registry_tag or '').split('/', 1)[0].strip()
    if not first:
        return None, None
    parsed = urlparse(f'//{first}')
    host = parsed.hostname
    port = parsed.port or 443
    return host, port


def _log_registry_diagnostics(registry_tag):
    host, port = _registry_host_port_from_tag(registry_tag)
    if not host:
        logger.warning("Registry diagnostics skipped: could not parse host from tag=%r", registry_tag)
        return

    logger.warning("Registry diagnostics: host=%s port=%s tag=%s", host, port, registry_tag)

    # DNS resolution diagnostics.
    try:
        addrinfo = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        resolved = sorted({entry[4][0] for entry in addrinfo if entry and entry[4]})
        logger.warning("Registry diagnostics: resolved addresses=%s", resolved)
    except OSError as e:
        logger.warning("Registry diagnostics: DNS resolution failed for %s:%s: %s", host, port, e)

    # TCP connectivity diagnostics.
    try:
        with socket.create_connection((host, port), timeout=5):
            logger.warning("Registry diagnostics: TCP connect to %s:%s succeeded", host, port)
    except OSError as e:
        logger.warning("Registry diagnostics: TCP connect to %s:%s failed: %s", host, port, e)

    # HTTP registry endpoint diagnostics.
    http_url = f'http://{host}:{port}/v2/'
    try:
        probe = subprocess.run(
            [
                'curl',
                '--noproxy',
                '*',
                '-sS',
                '-o',
                '/dev/null',
                '-w',
                '%{http_code}',
                '--max-time',
                '5',
                http_url,
            ],
            capture_output=True,
            text=True,
            timeout=7,
            check=False,
        )
        code = (probe.stdout or '').strip()
        err = (probe.stderr or '').strip()
        logger.warning("Registry diagnostics: curl %s -> http_code=%s stderr=%r", http_url, code, err)
    except Exception as e:
        logger.warning("Registry diagnostics: curl probe failed for %s: %s", http_url, e)


def list_vms():
    """Returns list of dicts from tart list --format json."""
    output = _run(['list', '--format', 'json'], timeout=10)
    return json.loads(output) if output else []


def get_vm_ip(vm_name, wait=5):
    """Get IP of a running VM. Returns None if not available."""
    try:
        ip = _run(['ip', vm_name, '--wait', str(wait)], timeout=wait + 5)
        return ip if ip else None
    except (subprocess.TimeoutExpired, RuntimeError):
        return None


def create_vm(name, base_image):
    """Clone a VM from base image (tart clone). Can take up to 10 min for first pull."""
    _run(['clone', base_image, name], timeout=600)


def start_vm(name):
    """Start a VM with VNC enabled as a background subprocess."""
    logger.info("start_vm(%s): launching 'tart run --no-graphics --vnc %s'", name, name)
    proc = subprocess.Popen(
        ['tart', 'run', '--no-graphics', '--vnc', name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    time.sleep(1)
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode()
        raise RuntimeError(f"tart run failed immediately: {stderr}")
    logger.info("start_vm(%s): tart run process started (pid=%s)", name, proc.pid)
    return proc


def stop_vm(name, timeout=60):
    """Graceful shutdown. Waits up to timeout seconds for VM to stop."""
    try:
        _run(['stop', name], timeout=10)
    except RuntimeError:
        pass  # may already be stopped
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Some Tart versions emit keys as Name/State instead of name/state.
        vms = {(v.get('name') or v.get('Name')): v for v in list_vms()}
        vm = vms.get(name)
        state = (vm.get('state') or vm.get('State') or '').lower() if vm else ''
        if not vm or state != 'running':
            return True
        time.sleep(2)
    return False  # timed out


def push_vm(name, registry_tag, progress_cb=None):
    """Push VM disk to registry. Blocking — can take many minutes."""
    preferred_insecure = bool(agent_config.REGISTRY_INSECURE)
    attempts = [preferred_insecure]
    if attempts[0] is True:
        attempts.append(False)
    else:
        attempts.append(True)

    def _progress(line, parsed):
        logger.info("push_vm(%s) progress: %s", name, line)
        _notify_progress(progress_cb, line, parsed)

    last_error = None
    for use_insecure in attempts:
        args = ['push', name, registry_tag]
        if use_insecure:
            args.append('--insecure')
        logger.warning("push_vm(%s) running command: tart %s", name, ' '.join(args))
        try:
            _run_with_progress(args, timeout=3600, progress_cb=_progress if progress_cb else None)
            return
        except RuntimeError as e:
            last_error = e
            logger.warning(
                "push_vm(%s) failed with insecure=%s, will %sretry: %s",
                name,
                use_insecure,
                '' if use_insecure != attempts[-1] else 'not ',
                e,
            )
            _log_registry_diagnostics(registry_tag)
    raise RuntimeError(str(last_error))


def pull_vm(registry_tag, local_name, progress_cb=None):
    """
    Pull VM image from registry and ensure a local VM with local_name exists.

    Tart CLI behavior differs by version:
    - `tart pull <remote>` fetches image layers but may not create a local VM.
    - `tart clone <remote> <local>` creates a runnable local VM (and pulls if needed).
    This helper handles both cases.
    """
    def _progress(line, parsed):
        logger.info("pull_vm(%s) progress: %s", local_name, line)
        _notify_progress(progress_cb, line, parsed)

    preferred_insecure = bool(agent_config.REGISTRY_INSECURE)
    attempts = [preferred_insecure]
    if attempts[0] is True:
        attempts.append(False)
    else:
        attempts.append(True)

    # Kill any orphaned tart pull processes for this tag that hold the image lock.
    _kill_stale_tart_pulls(registry_tag)

    # Stage 1: pull remote image layers.
    last_error = None
    for use_insecure in attempts:
        args = ['pull', registry_tag]
        if use_insecure:
            args.append('--insecure')
        logger.warning(
            "pull_vm(tag=%s, local=%s) running command: tart %s",
            registry_tag,
            local_name,
            ' '.join(args),
        )
        try:
            _run_with_progress(args, timeout=3600, progress_cb=_progress if progress_cb else None)
            break
        except RuntimeError as e:
            last_error = e
            logger.warning(
                "pull_vm(tag=%s, local=%s) failed with insecure=%s, will %sretry: %s",
                registry_tag,
                local_name,
                use_insecure,
                '' if use_insecure != attempts[-1] else 'not ',
                e,
            )
            _log_registry_diagnostics(registry_tag)
    else:
        raise RuntimeError(str(last_error))

    # If pull already resulted in a local VM with the desired name, we're done.
    if vm_exists(local_name):
        return

    # Stage 2: create a local runnable VM name from the remote image.
    _notify_progress(
        progress_cb,
        f'Pull complete, local VM "{local_name}" missing. Falling back to clone.',
        {'status': 'cloning'},
    )
    clone_error = None
    for use_insecure in attempts:
        args = ['clone', registry_tag, local_name]
        if use_insecure:
            args.append('--insecure')
        logger.warning(
            "pull_vm(tag=%s, local=%s) local VM missing after pull; running tart %s",
            registry_tag,
            local_name,
            ' '.join(args),
        )
        try:
            _run_with_progress(args, timeout=3600, progress_cb=_progress if progress_cb else None)
            if not vm_exists(local_name):
                raise RuntimeError(
                    f'tart clone completed but local VM "{local_name}" was not found'
                )
            return
        except RuntimeError as e:
            clone_error = e
            logger.warning(
                "clone fallback failed for tag=%s local=%s insecure=%s: %s",
                registry_tag,
                local_name,
                use_insecure,
                e,
            )
            _log_registry_diagnostics(registry_tag)

    raise RuntimeError(str(clone_error or last_error))


def delete_vm(name):
    """
    Delete local VM (frees disk space).
    Tart can report "does not exist" for running VMs, so always stop first.
    """
    # Ensure VM is not running before delete.
    stop_vm(name)

    try:
        _run(['delete', name], timeout=30)
    except RuntimeError as e:
        # If Tart says missing and VM is truly absent, treat as success.
        if not vm_exists(name):
            return
        raise RuntimeError(str(e))

    # Verify VM is gone after delete.
    if vm_exists(name):
        raise RuntimeError(f'tart delete reported success but VM "{name}" still exists')


def vm_exists(name):
    vms = {(v.get('name') or v.get('Name')) for v in list_vms()}
    return name in vms
