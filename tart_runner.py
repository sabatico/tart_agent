import json
import logging
import subprocess
import time
import socket
from urllib.parse import urlparse
import agent_config

logger = logging.getLogger(__name__)


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


def push_vm(name, registry_tag):
    """Push VM disk to registry. Blocking — can take many minutes."""
    preferred_insecure = bool(agent_config.REGISTRY_INSECURE)
    attempts = [preferred_insecure]
    if attempts[0] is True:
        attempts.append(False)
    else:
        attempts.append(True)

    last_error = None
    for use_insecure in attempts:
        args = ['push', name, registry_tag]
        if use_insecure:
            args.append('--insecure')
        logger.warning("push_vm(%s) running command: tart %s", name, ' '.join(args))
        try:
            _run(args, timeout=3600)
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


def pull_vm(registry_tag, local_name):
    """Pull VM disk from registry into the requested local VM name."""
    preferred_insecure = bool(agent_config.REGISTRY_INSECURE)
    attempts = [preferred_insecure]
    if attempts[0] is True:
        attempts.append(False)
    else:
        attempts.append(True)

    last_error = None
    for use_insecure in attempts:
        args = ['pull', registry_tag, local_name]
        if use_insecure:
            args.append('--insecure')
        logger.warning(
            "pull_vm(tag=%s, local=%s) running command: tart %s",
            registry_tag,
            local_name,
            ' '.join(args),
        )
        try:
            _run(args, timeout=3600)
            if not vm_exists(local_name):
                raise RuntimeError(
                    f'tart pull completed but local VM "{local_name}" was not found'
                )
            return
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
    raise RuntimeError(str(last_error))


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
