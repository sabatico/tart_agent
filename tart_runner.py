import json
import logging
import subprocess
import time

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
    proc = subprocess.Popen(
        ['tart', 'run', '--no-graphics', '--vnc', name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    time.sleep(1)
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode()
        raise RuntimeError(f"tart run failed immediately: {stderr}")
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
    _run(['push', name, registry_tag], timeout=3600)


def pull_vm(registry_tag, name):
    """Pull VM disk from registry. Blocking — can take many minutes."""
    _run(['pull', registry_tag, name], timeout=3600)


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
