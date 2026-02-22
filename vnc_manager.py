"""
VNC proxy manager for the TART Agent.
Manages websockify subprocesses on the local Mac node.
One websockify process per running VM that needs a console.
"""
import logging
import socket
import subprocess
import threading
import time

import agent_config

logger = logging.getLogger(__name__)


class VncManager:
    """
    Thread-safe manager for websockify subprocess lifecycles.

    Each VM that a user wants to VNC into gets its own websockify process:
        browser WebSocket (ws://node:<port>) → VM VNC (tcp://<vm_ip>:5900)
    """

    def __init__(self):
        # vm_name → {process, port, vm_ip, started_at}
        self._proxies = {}
        self._lock = threading.Lock()

    def _find_free_port(self):
        with self._lock:
            used = {info['port'] for info in self._proxies.values()}
        for port in range(agent_config.WEBSOCKIFY_PORT_MIN,
                          agent_config.WEBSOCKIFY_PORT_MAX + 1):
            if port in used:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('0.0.0.0', port))
                    return port
            except OSError:
                continue
        raise RuntimeError(
            f'No free websockify ports in range '
            f'{agent_config.WEBSOCKIFY_PORT_MIN}-{agent_config.WEBSOCKIFY_PORT_MAX}'
        )

    def start_proxy(self, vm_name, target_host):
        """
        Start websockify for a VM. Reuses existing process if alive.
        Returns the local port number.
        """
        with self._lock:
            if vm_name in self._proxies:
                info = self._proxies[vm_name]
                if info['process'].poll() is None:
                    return info['port']
                del self._proxies[vm_name]

        port = self._find_free_port()
        target = f'{target_host}:{agent_config.VNC_PORT}'
        cmd = [agent_config.WEBSOCKIFY_BIN, str(port), target]

        logger.info("Starting websockify: port %d → %s (vm=%s)", port, target, vm_name)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"websockify binary not found at '{agent_config.WEBSOCKIFY_BIN}'"
            )

        time.sleep(0.3)
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode() if proc.stderr else ''
            raise RuntimeError(f"websockify failed to start for {vm_name}: {stderr}")

        def _drain_stderr():
            if not proc.stderr:
                return
            try:
                while True:
                    line = proc.stderr.readline()
                    if not line:
                        break
                    msg = line.decode(errors='replace').strip()
                    if msg:
                        logger.info("websockify[%s]: %s", vm_name, msg)
            except Exception:
                pass

        threading.Thread(target=_drain_stderr, daemon=True).start()

        with self._lock:
            self._proxies[vm_name] = {
                'process': proc,
                'port': port,
                'target_host': target_host,
                'started_at': time.time(),
            }

        logger.info("websockify started for %s on port %d (pid=%d)", vm_name, port, proc.pid)
        return port

    def stop_proxy(self, vm_name):
        """Stop websockify for a VM. Safe to call if not running."""
        with self._lock:
            info = self._proxies.pop(vm_name, None)
        if info is None:
            return
        proc = info['process']
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        logger.info("websockify stopped for %s", vm_name)

    def get_proxy_port(self, vm_name):
        with self._lock:
            info = self._proxies.get(vm_name)
            if info is None:
                return None
            if info['process'].poll() is not None:
                del self._proxies[vm_name]
                return None
            return info['port']

    def cleanup_all(self):
        with self._lock:
            names = list(self._proxies.keys())
        for name in names:
            self.stop_proxy(name)
